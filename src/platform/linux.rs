use anyhow::{anyhow, Context, Result};
use dashmap::DashMap;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::time::{timeout, Duration};
use uuid::Uuid;

use atspi::{
    cache::{CacheItem, LegacyCacheItem},
    proxy::accessible::AccessibleProxy, proxy::action::ActionProxy,
    proxy::application::ApplicationProxy, proxy::component::ComponentProxy,
    proxy::editable_text::EditableTextProxy, proxy::text::TextProxy, proxy::value::ValueProxy,
    CoordType, Interface, Role, State,
};
use zbus::names::BusName;
use zbus::zvariant::ObjectPath;
use zbus::{CacheProperties, Connection};

use crate::{
    platform::UiBackend,
    types::{ElementType, ExpandState, RangeInfo, Rect, ToggleState, UiElement, WindowInfo},
};

// ── Element registry ──────────────────────────────────────────────────────────

struct StoredElement {
    bus_name: String,
    object_path: String,
}

type IdRegistry = Arc<DashMap<String, StoredElement>>;

// ── Helpers for zbus type conversions ─────────────────────────────────────────

fn bus_name(s: &str) -> BusName<'_> {
    BusName::try_from(s).unwrap_or_else(|_| BusName::try_from(":0.0").unwrap())
}

fn obj_path(s: &str) -> ObjectPath<'_> {
    ObjectPath::try_from(s).unwrap_or_else(|_| ObjectPath::try_from("/").unwrap())
}

// ── Backend ───────────────────────────────────────────────────────────────────

pub struct LinuxUiBackend {
    connection: Connection,
    registry: IdRegistry,
    rt: Arc<tokio::runtime::Runtime>,
}

impl Drop for LinuxUiBackend {
    fn drop(&mut self) {
        let rt = Arc::clone(&self.rt);
        std::thread::spawn(move || drop(rt));
    }
}

impl LinuxUiBackend {
    pub fn new() -> Result<Self> {
        fn atspi_init_thread() -> Result<(tokio::runtime::Runtime, Connection)> {
            let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .context("Failed to create dedicated Tokio runtime for AT-SPI2")?;

            let connection = rt.block_on(async {
                let atspi_address: String = {
                    let session = Connection::session()
                        .await
                        .context("Failed to connect to D-Bus session bus")?;
            
                    let addr: String = session
                        .call_method(
                            Some("org.a11y.Bus"),
                            "/org/a11y/bus",
                            Some("org.a11y.Bus"),
                            "GetAddress",
                            &(),
                        )
                        .await
                        .context("Failed to get AT-SPI bus address from org.a11y.Bus")?
                        .body::<String>()
                        .context("Failed to deserialize AT-SPI bus address")?;
            
                    drop(session); // explicitly drop before connecting to AT-SPI bus
                    addr
                };
            
                tracing::info!("Connecting to AT-SPI2 bus at {}", atspi_address);
            
                zbus::ConnectionBuilder::address(atspi_address.as_str())?
                    .build()
                    .await
                    .context("Failed to connect to AT-SPI2 accessibility bus")
            })?;

            Ok((rt, connection))
        }

        let handle = std::thread::spawn(atspi_init_thread);

        let (rt, connection) = handle
            .join()
            .map_err(|_| anyhow!("AT-SPI2 init thread panicked"))??;

        tracing::info!("Connected to AT-SPI2 accessibility bus");

        Ok(Self {
            connection,
            registry: Arc::new(DashMap::new()),
            rt: Arc::new(rt),
        })
    }

    // ── Role → ElementType mapping ────────────────────────────────────────

    fn role_to_element_type(role: Role) -> ElementType {
        match role {
            Role::Frame | Role::Window => ElementType::Window,
            Role::PushButton | Role::ToggleButton => ElementType::Button,
            Role::Text | Role::Entry | Role::PasswordText | Role::SpinButton => ElementType::Edit,
            Role::Label | Role::Static | Role::Heading | Role::Paragraph => ElementType::Text,
            Role::CheckBox | Role::CheckMenuItem => ElementType::CheckBox,
            Role::RadioButton | Role::RadioMenuItem => ElementType::RadioButton,
            Role::ComboBox => ElementType::ComboBox,
            Role::List => ElementType::ListBox,
            Role::ListItem => ElementType::ListItem,
            Role::Tree | Role::TreeTable => ElementType::TreeView,
            Role::TreeItem => ElementType::TreeItem,
            Role::Menu | Role::MenuBar => ElementType::Menu,
            Role::MenuItem => ElementType::MenuItem,
            Role::PageTabList => ElementType::TabControl,
            Role::PageTab => ElementType::TabItem,
            Role::ToolBar => ElementType::ToolBar,
            Role::StatusBar => ElementType::StatusBar,
            Role::ScrollBar => ElementType::ScrollBar,
            Role::Slider => ElementType::Slider,
            Role::ProgressBar => ElementType::ProgressBar,
            Role::Image | Role::Icon => ElementType::Image,
            Role::Link => ElementType::Link,
            Role::Panel | Role::Filler => ElementType::Group,
            Role::ScrollPane => ElementType::Pane,
            Role::Dialog | Role::Alert | Role::FileChooser => ElementType::Dialog,
            Role::DocumentWeb => ElementType::Document,
            Role::Table => ElementType::Table,
            _ => ElementType::Unknown,
        }
    }

    // ── Async helpers ─────────────────────────────────────────────────────

    async fn make_accessible_proxy<'a>(
        conn: &'a Connection,
        bname: &'a str,
        opath: &'a str,
    ) -> Result<AccessibleProxy<'a>> {
        AccessibleProxy::builder(conn)
            .destination(bus_name(bname))?
            .path(obj_path(opath))?
            .cache_properties(CacheProperties::No)
            .build()
            .await
            .context("Failed to build AccessibleProxy")
    }

    async fn make_component_proxy<'a>(
        conn: &'a Connection,
        bname: &'a str,
        opath: &'a str,
    ) -> Result<ComponentProxy<'a>> {
        ComponentProxy::builder(conn)
            .destination(bus_name(bname))?
            .path(obj_path(opath))?
            .cache_properties(CacheProperties::No)
            .build()
            .await
            .context("Failed to build ComponentProxy")
    }

    async fn make_action_proxy<'a>(
        conn: &'a Connection,
        bname: &'a str,
        opath: &'a str,
    ) -> Result<ActionProxy<'a>> {
        ActionProxy::builder(conn)
            .destination(bus_name(bname))?
            .path(obj_path(opath))?
            .cache_properties(CacheProperties::No)
            .build()
            .await
            .context("Failed to build ActionProxy")
    }

    async fn make_application_proxy<'a>(
        conn: &'a Connection,
        bname: &'a str,
        opath: &'a str,
    ) -> Result<ApplicationProxy<'a>> {
        ApplicationProxy::builder(conn)
            .destination(bus_name(bname))?
            .path(obj_path(opath))?
            .cache_properties(CacheProperties::No)
            .build()
            .await
            .context("Failed to build ApplicationProxy")
    }

    async fn make_text_proxy<'a>(
        conn: &'a Connection,
        bname: &'a str,
        opath: &'a str,
    ) -> Result<TextProxy<'a>> {
        TextProxy::builder(conn)
            .destination(bus_name(bname))?
            .path(obj_path(opath))?
            .cache_properties(CacheProperties::No)
            .build()
            .await
            .context("Failed to build TextProxy")
    }

    async fn make_value_proxy<'a>(
        conn: &'a Connection,
        bname: &'a str,
        opath: &'a str,
    ) -> Result<ValueProxy<'a>> {
        ValueProxy::builder(conn)
            .destination(bus_name(bname))?
            .path(obj_path(opath))?
            .cache_properties(CacheProperties::No)
            .build()
            .await
            .context("Failed to build ValueProxy")
    }

    async fn make_editable_text_proxy<'a>(
        conn: &'a Connection,
        bname: &'a str,
        opath: &'a str,
    ) -> Result<EditableTextProxy<'a>> {
        EditableTextProxy::builder(conn)
            .destination(bus_name(bname))?
            .path(obj_path(opath))?
            .cache_properties(CacheProperties::No)
            .build()
            .await
            .context("Failed to build EditableTextProxy")
    }

    // ── Build element ─────────────────────────────────────────────────────

    async fn build_element_async(
        &self,
        bname: &str,
        opath: &str,
        with_children: bool,
        depth: u32,
    ) -> Result<UiElement> {
        let zero_rect = Rect {
            x: 0,
            y: 0,
            width: 0,
            height: 0,
        };

        if depth > 48 {
            let id = Uuid::new_v4().to_string();
            self.registry.insert(
                id.clone(),
                StoredElement {
                    bus_name: bname.to_string(),
                    object_path: opath.to_string(),
                },
            );
            return Ok(UiElement {
                oculos_id: id,
                element_type: ElementType::Unknown,
                label: String::new(),
                value: None,
                text_content: None,
                rect: zero_rect,
                enabled: false,
                focused: false,
                is_keyboard_focusable: false,
                toggle_state: None,
                is_selected: None,
                expand_state: None,
                range: None,
                automation_id: None,
                class_name: None,
                help_text: None,
                keyboard_shortcut: None,
                actions: vec![],
                children: vec![],
            });
        }

        let proxy = Self::make_accessible_proxy(&self.connection, bname, opath).await?;

        let name = proxy.name().await.unwrap_or_default();
        let role = proxy.get_role().await.unwrap_or(Role::Invalid);
        let element_type = Self::role_to_element_type(role);

        // State set
        let states = proxy.get_state().await.unwrap_or_default();
        let enabled = states.contains(State::Enabled);
        let focused = states.contains(State::Focused);
        let is_keyboard_focusable = states.contains(State::Focusable);
        let is_selected_state = states.contains(State::Selected);
        let is_checked = states.contains(State::Checked);
        let is_expanded = states.contains(State::Expanded);
        let is_expandable = states.contains(State::Expandable);

        // Bounding box via Component interface
        let rect =
            if let Ok(comp) = Self::make_component_proxy(&self.connection, bname, opath).await {
                if let Ok(extents) = comp.get_extents(CoordType::Screen).await {
                    Rect {
                        x: extents.0,
                        y: extents.1,
                        width: extents.2,
                        height: extents.3,
                    }
                } else {
                    zero_rect
                }
            } else {
                zero_rect
            };

        // Value (for text fields, sliders, etc.)
        let value = self.get_text_value(bname, opath).await;

        let toggle_state = if element_type == ElementType::CheckBox {
            Some(if is_checked {
                ToggleState::On
            } else {
                ToggleState::Off
            })
        } else {
            None
        };

        let is_selected = if is_selected_state { Some(true) } else { None };

        let expand_state = if is_expandable {
            Some(if is_expanded {
                ExpandState::Expanded
            } else {
                ExpandState::Collapsed
            })
        } else {
            None
        };

        // Only query Value interface for element types that actually use ranges.
        // Chrome advertises Value on elements that don't implement it, causing
        // noisy ATK assertion spam: "impl_get_CurrentValue: assertion 'ATK_IS_VALUE'"
        let range = match element_type {
            ElementType::Slider
            | ElementType::SpinButton
            | ElementType::ProgressBar
            | ElementType::ScrollBar => self.get_range_info(bname, opath).await,
            _ => None,
        };
        let actions = self
            .collect_actions(bname, opath, &element_type, is_keyboard_focusable)
            .await;
        let help_text = proxy.description().await.ok().filter(|s| !s.is_empty());

        // Children
        let children = if with_children {
            let child_count = proxy.child_count().await.unwrap_or(0);
            let mut kids = Vec::with_capacity(child_count as usize);
            for i in 0..child_count {
                if let Ok(child) = proxy.get_child_at_index(i).await {
                    let cb = child.name.clone();
                    let cp = child.path.to_string();
                    if let Ok(elem) =
                        Box::pin(self.build_element_async(&cb, &cp, true, depth + 1)).await
                    {
                        kids.push(elem);
                    }
                }
            }
            kids
        } else {
            vec![]
        };

        let oculos_id = Uuid::new_v4().to_string();
        self.registry.insert(
            oculos_id.clone(),
            StoredElement {
                bus_name: bname.to_string(),
                object_path: opath.to_string(),
            },
        );

        Ok(UiElement {
            oculos_id,
            element_type,
            label: name,
            value,
            text_content: None,
            rect,
            enabled,
            focused,
            is_keyboard_focusable,
            toggle_state,
            is_selected,
            expand_state,
            range,
            automation_id: None,
            class_name: None,
            help_text,
            keyboard_shortcut: None,
            actions,
            children,
        })
    }

    async fn get_text_value(&self, bname: &str, opath: &str) -> Option<String> {
        let tp = Self::make_text_proxy(&self.connection, bname, opath)
            .await
            .ok()?;
        let cc = tp.character_count().await.ok()?;
        if cc == 0 {
            return None;
        }
        tp.get_text(0, cc).await.ok().filter(|s| !s.is_empty())
    }

    async fn get_range_info(&self, bname: &str, opath: &str) -> Option<RangeInfo> {
        let vp = Self::make_value_proxy(&self.connection, bname, opath)
            .await
            .ok()?;
        let current = vp.current_value().await.ok()?;
        let minimum = vp.minimum_value().await.unwrap_or(0.0);
        let maximum = vp.maximum_value().await.unwrap_or(100.0);
        let step = vp.minimum_increment().await.unwrap_or(1.0);
        Some(RangeInfo {
            value: current,
            minimum,
            maximum,
            step,
            read_only: false,
        })
    }

    async fn collect_actions(
        &self,
        bname: &str,
        opath: &str,
        element_type: &ElementType,
        focusable: bool,
    ) -> Vec<String> {
        let mut actions = Vec::new();

        if let Ok(ap) = Self::make_action_proxy(&self.connection, bname, opath).await {
            if let Ok(action_list) = ap.get_actions().await {
                for (i, (name, _desc, _kb)) in action_list.iter().enumerate() {
                    match name.as_str() {
                        "click" | "press" | "activate" => {
                            if !actions.contains(&"click".to_string()) {
                                actions.push("click".into());
                            }
                        }
                        "toggle" => actions.push("toggle".into()),
                        "expand or contract" | "expand" => actions.push("expand".into()),
                        "collapse" => actions.push("collapse".into()),
                        _ => {}
                    }
                    let _ = i;
                }
            }
        }

        if Self::make_editable_text_proxy(&self.connection, bname, opath)
            .await
            .is_ok()
        {
            actions.push("set-text".into());
            actions.push("send-keys".into());
        }

        if matches!(element_type, ElementType::Slider | ElementType::ProgressBar) {
            if Self::make_value_proxy(&self.connection, bname, opath)
                .await
                .is_ok()
            {
                actions.push("set-range".into());
            }
        }

        if focusable {
            actions.push("focus".into());
        }

        actions
    }

    // ── Cache-based search ──────────────────────────────────────────────

    /// Build a UiElement from cached data, only fetching bounds/text/actions
    /// via D-Bus for this specific element. Much cheaper than build_element_async
    /// which also traverses children.
    async fn build_element_from_cache(&self, ci: &CacheItem) -> Result<UiElement> {
        let bname = ci.object.name.as_str();
        let opath = ci.object.path.as_str();
        let element_type = Self::role_to_element_type(ci.role);

        // States from cache (zero D-Bus calls)
        let enabled = ci.states.contains(State::Enabled);
        let focused = ci.states.contains(State::Focused);
        let is_keyboard_focusable = ci.states.contains(State::Focusable);
        let is_selected_state = ci.states.contains(State::Selected);
        let is_checked = ci.states.contains(State::Checked);
        let is_expanded = ci.states.contains(State::Expanded);
        let is_expandable = ci.states.contains(State::Expandable);

        // Bounding box — needs D-Bus (1 call, only if Component interface present)
        let rect = if ci.ifaces.contains(Interface::Component) {
            self.get_component_rect(bname, opath).await
        } else {
            Rect { x: 0, y: 0, width: 0, height: 0 }
        };

        // Text value — only if Text interface present (1-2 calls)
        let value = if ci.ifaces.contains(Interface::Text) {
            self.get_text_value(bname, opath).await
        } else {
            None
        };

        let toggle_state = if element_type == ElementType::CheckBox {
            Some(if is_checked { ToggleState::On } else { ToggleState::Off })
        } else {
            None
        };

        let is_selected = if is_selected_state { Some(true) } else { None };

        let expand_state = if is_expandable {
            Some(if is_expanded { ExpandState::Expanded } else { ExpandState::Collapsed })
        } else {
            None
        };

        // Range — only if Value interface present (1-4 calls)
        let range = if ci.ifaces.contains(Interface::Value) {
            self.get_range_info(bname, opath).await
        } else {
            None
        };

        // Actions — only if Action or EditableText interface present
        let actions = if ci.ifaces.contains(Interface::Action)
            || ci.ifaces.contains(Interface::EditableText)
            || ci.ifaces.contains(Interface::Value)
        {
            self.collect_actions(bname, opath, &element_type, is_keyboard_focusable)
                .await
        } else if is_keyboard_focusable {
            vec!["focus".into()]
        } else {
            vec![]
        };

        let help_text = if ci.name != ci.short_name && !ci.short_name.is_empty() {
            Some(ci.short_name.clone())
        } else {
            None
        };

        let oculos_id = Uuid::new_v4().to_string();
        self.registry.insert(
            oculos_id.clone(),
            StoredElement {
                bus_name: bname.to_string(),
                object_path: opath.to_string(),
            },
        );

        Ok(UiElement {
            oculos_id,
            element_type,
            label: ci.name.clone(),
            value,
            text_content: None,
            rect,
            enabled,
            focused,
            is_keyboard_focusable,
            toggle_state,
            is_selected,
            expand_state,
            range,
            automation_id: None,
            class_name: None,
            help_text,
            keyboard_shortcut: None,
            actions,
            children: vec![],
        })
    }

    async fn search_elements_cached(
        &self,
        app_bus_name: &str,
        root_path: &str,
        query: Option<&str>,
        element_type: Option<&ElementType>,
        interactive_only: bool,
    ) -> Result<Vec<UiElement>> {
        // Try cache-based search first (1 D-Bus call for all elements)
        match Self::fetch_cache_items(&self.connection, app_bus_name).await {
            Ok(items) => {
                tracing::info!(
                    "Cache search: {} items from {} (root={})",
                    items.len(),
                    app_bus_name,
                    root_path
                );
                let cache = AppCache::from_items(items);
                let mut results = Vec::new();
                let query_lower = query.map(|q| q.to_lowercase());
    
                // DFS traversal entirely in-memory
                let mut stack = vec![root_path.to_string()];
                while let Some(path) = stack.pop() {
                    if results.len() >= 500 {
                        break;
                    }
                    if let Some(ci) = cache.items.get(&path) {
                        let mut matches = true;
                        if let Some(ref q) = query_lower {
                            let label_match = ci.name.to_lowercase().contains(q.as_str());
                            let short_match = ci.short_name.to_lowercase().contains(q.as_str());
                            if !label_match && !short_match {
                                matches = false;
                            }
                        }
                        if let Some(wanted) = element_type {
                            if &Self::role_to_element_type(ci.role) != wanted {
                                matches = false;
                            }
                        }
                        if interactive_only {
                            let has_interaction = ci.ifaces.contains(Interface::Action)
                                || ci.ifaces.contains(Interface::EditableText)
                                || ci.ifaces.contains(Interface::Value);
                            if !has_interaction {
                                matches = false;
                            }
                        }
                        if matches {
                            match self.build_element_from_cache(ci).await {
                                Ok(elem) => results.push(elem),
                                Err(e) => tracing::debug!("Skipping element {}: {}", path, e),
                            }
                        }
                        for child_path in cache.children(&path) {
                            stack.push(child_path.clone());
                        }
                    }
                }
                Ok(results)
            }
            Err(e) => {
                // Cache not available (e.g. Chrome) — fall back to per-element
                // traversal with a hard timeout to prevent hangs/crashes
                tracing::warn!(
                    "Cache unavailable for {} ({}), falling back to traversal with 10s timeout",
                    app_bus_name, e
                );
                let mut results = Vec::new();
                match tokio::time::timeout(
                    std::time::Duration::from_secs(10),
                    self.search_elements_async(
                        app_bus_name,
                        root_path,
                        query,
                        element_type,
                        interactive_only,
                        &mut results,
                        0,
                    ),
                )
                .await
                {
                    Ok(_) => {
                        tracing::info!(
                            "Traversal found {} results for {}",
                            results.len(),
                            app_bus_name
                        );
                    }
                    Err(_) => {
                        tracing::warn!(
                            "Traversal timed out for {}, returning {} partial results",
                            app_bus_name,
                            results.len()
                        );
                    }
                }
                Ok(results)
            }
        }
    }
    

    // ── Legacy per-element search (fallback) ─────────────────────────────

    async fn search_elements_async(
        &self,
        bname: &str,
        opath: &str,
        query: Option<&str>,
        element_type: Option<&ElementType>,
        interactive_only: bool,
        results: &mut Vec<UiElement>,
        depth: u32,
    ) {
        if depth > 48 || results.len() >= 500 {
            return;
        }

        if let Ok(elem) = self.build_element_async(bname, opath, false, depth).await {
            let query_lower = query.map(|q| q.to_lowercase());
            let mut matches = true;

            if let Some(ref q) = query_lower {
                let label_match = elem.label.to_lowercase().contains(q.as_str());
                let aid_match = elem
                    .automation_id
                    .as_ref()
                    .map(|a| a.to_lowercase().contains(q.as_str()))
                    .unwrap_or(false);
                if !label_match && !aid_match {
                    matches = false;
                }
            }
            if let Some(wanted) = element_type {
                if &elem.element_type != wanted {
                    matches = false;
                }
            }
            if interactive_only && elem.actions.is_empty() {
                matches = false;
            }
            if matches {
                results.push(elem);
            }
        }

        if let Ok(proxy) = Self::make_accessible_proxy(&self.connection, bname, opath).await {
            let child_count = proxy.child_count().await.unwrap_or(0);
            for i in 0..child_count {
                if let Ok(child) = proxy.get_child_at_index(i).await {
                    let cb = child.name.clone();
                    let cp = child.path.to_string();
                    Box::pin(self.search_elements_async(
                        &cb,
                        &cp,
                        query,
                        element_type,
                        interactive_only,
                        results,
                        depth + 1,
                    ))
                    .await;
                }
            }
        }
    }

    // ── Find app root for a PID ───────────────────────────────────────────

    async fn find_app_root(&self, pid: u32) -> Result<(String, String)> {
        // Use GetChildren directly — child_count() returns 0 on this atspi version.
        let children: Vec<(String, zbus::zvariant::OwnedObjectPath)> = self
            .connection
            .call_method(
                Some("org.a11y.atspi.Registry"),
                "/org/a11y/atspi/accessible/root",
                Some("org.a11y.atspi.Accessible"),
                "GetChildren",
                &(),
            )
            .await
            .context("Failed to call GetChildren on AT-SPI registry")?
            .body::<Vec<(String, zbus::zvariant::OwnedObjectPath)>>()
            .context("Failed to deserialize children")?;

        for (cb, cp) in &children {
            if self.get_dbus_pid(cb.as_str()).await == Some(pid) {
                return Ok((cb.clone(), cp.as_str().to_string()));
            }
        }

        Err(anyhow!("No AT-SPI2 application found for PID {}", pid))
    }

    async fn get_component_rect(&self, bname: &str, opath: &str) -> Rect {
        if let Ok(comp) = Self::make_component_proxy(&self.connection, bname, opath).await {
            if let Ok(extents) = comp.get_extents(CoordType::Screen).await {
                return Rect {
                    x: extents.0,
                    y: extents.1,
                    width: extents.2,
                    height: extents.3,
                };
            }
        }
        Rect {
            x: 0,
            y: 0,
            width: 0,
            height: 0,
        }
    }

    // ── Sync wrappers ─────────────────────────────────────────────────────

    fn block_on<F: std::future::Future<Output = T>, T>(&self, f: F) -> T {
        self.rt.block_on(f)
    }

    /// Get the real Unix PID for a D-Bus connection by asking the bus daemon.
    /// Unlike org.a11y.atspi.Application.Id (which apps can set to anything),
    /// this returns the actual OS process ID tracked by D-Bus itself.
    async fn get_dbus_pid(&self, bus_name: &str) -> Option<u32> {
        self.connection
            .call_method(
                Some("org.freedesktop.DBus"),
                "/org/freedesktop/DBus",
                Some("org.freedesktop.DBus"),
                "GetConnectionUnixProcessID",
                &(bus_name,),
            )
            .await
            .ok()
            .and_then(|msg| msg.body::<u32>().ok())
    }


    // ── AT-SPI Cache bulk fetch ──────────────────────────────────────────

    /// How long to wait for a single app's Cache.GetItems() response.
    const CACHE_TIMEOUT: Duration = Duration::from_secs(5);

    /// Bulk-fetch all accessible objects for an app via org.a11y.atspi.Cache.GetItems().
    /// Returns one CacheItem per element in the app's accessibility tree — all in a single
    /// D-Bus call instead of N×10 individual round-trips.
    async fn fetch_cache_items(conn: &Connection, app_bus_name: &str) -> Result<Vec<CacheItem>> {
        let msg = timeout(
            Self::CACHE_TIMEOUT,
            conn.call_method(
                Some(app_bus_name),
                "/org/a11y/atspi/cache",
                Some("org.a11y.atspi.Cache"),
                "GetItems",
                &(),
            ),
        )
        .await
        .map_err(|_| anyhow!("Cache.GetItems timed out for {}", app_bus_name))??;

        // Modern format: ((so)(so)(so)iiassusau)  — used by Chrome, GTK apps
        if let Ok(items) = msg.body::<Vec<CacheItem>>() {
            return Ok(items);
        }
        // Legacy format: ((so)(so)(so)a(so)assusau) — used by Qt apps
        if let Ok(legacy) = msg.body::<Vec<LegacyCacheItem>>() {
            // Convert legacy items to modern CacheItem
            let items = legacy
                .into_iter()
                .map(|l| CacheItem {
                    object: l.object,
                    app: l.app,
                    parent: l.parent,
                    index: 0,
                    children: l.children.len() as i32,
                    ifaces: l.ifaces,
                    short_name: l.short_name,
                    role: l.role,
                    name: l.name,
                    states: l.states,
                })
                .collect();
            return Ok(items);
        }
        Err(anyhow!(
            "Failed to deserialize Cache.GetItems for {}",
            app_bus_name
        ))
    }
}

/// In-memory index of an app's accessibility tree built from CacheItems.
/// Allows O(1) lookups and in-memory tree traversal with zero D-Bus calls.
struct AppCache {
    items: HashMap<String, CacheItem>,          // obj_path → CacheItem
    children_of: HashMap<String, Vec<String>>,  // parent_path → child obj_paths
}

impl AppCache {
    fn from_items(items: Vec<CacheItem>) -> Self {
        let mut map = HashMap::with_capacity(items.len());
        let mut children_of: HashMap<String, Vec<String>> = HashMap::new();
        for item in items {
            let obj_path = item.object.path.to_string();
            let parent_path = item.parent.path.to_string();
            children_of
                .entry(parent_path)
                .or_default()
                .push(obj_path.clone());
            map.insert(obj_path, item);
        }
        AppCache {
            items: map,
            children_of,
        }
    }

    /// Get direct children of a node, in order.
    fn children(&self, obj_path: &str) -> &[String] {
        self.children_of
            .get(obj_path)
            .map(|v| v.as_slice())
            .unwrap_or(&[])
    }
}

// ── UiBackend implementation ──────────────────────────────���───────────────────

impl UiBackend for LinuxUiBackend {
    fn list_windows(&self) -> Result<Vec<WindowInfo>> {
        self.block_on(async {
            // 1. Get registered app bus names from the AT-SPI registry
            let children: Vec<(String, zbus::zvariant::OwnedObjectPath)> = self
                .connection
                .call_method(
                    Some("org.a11y.atspi.Registry"),
                    "/org/a11y/atspi/accessible/root",
                    Some("org.a11y.atspi.Accessible"),
                    "GetChildren",
                    &(),
                )
                .await
                .context("Failed to call GetChildren on AT-SPI registry")?
                .body::<Vec<(String, zbus::zvariant::OwnedObjectPath)>>()
                .context("Failed to deserialize children")?;

            tracing::info!("Registry GetChildren returned {} children", children.len());

            let mut windows = Vec::new();

            for (cb, cp) in &children {
                let cp_str = cp.as_str();

                // 2. Get real OS PID (1 D-Bus call to the bus daemon, fast)
                let pid = self.get_dbus_pid(cb.as_str()).await.unwrap_or(0);

                // 3. Bulk-fetch the app's entire accessibility tree via Cache (1 D-Bus call)
                let cache = match Self::fetch_cache_items(&self.connection, cb.as_str()).await {
                    Ok(items) => {
                        tracing::debug!(
                            "Cache.GetItems for {} returned {} items",
                            cb,
                            items.len()
                        );
                        AppCache::from_items(items)
                    }
                    Err(e) => {
                        tracing::debug!("Cache.GetItems failed for {}: {}", cb, e);
                        // Fallback: try to at least get app name via proxy
                        if let Ok(p) = Self::make_accessible_proxy(&self.connection, cb, cp_str).await {
                            let app_name = p.name().await.unwrap_or_default();
                            if !app_name.is_empty() && pid > 0 {
                                windows.push(WindowInfo {
                                    pid,
                                    hwnd: 0,
                                    title: app_name.clone(),
                                    exe_name: app_name,
                                    rect: Rect { x: 0, y: 0, width: 0, height: 0 },
                                    visible: true,
                                });
                            }
                        }
                        continue;
                    }
                };

                // 4. Get app name from the root node in the cache
                let app_name = cache
                    .items
                    .get(cp_str)
                    .map(|ci| {
                        if ci.name.is_empty() {
                            ci.short_name.clone()
                        } else {
                            ci.name.clone()
                        }
                    })
                    .unwrap_or_default();
                if app_name.is_empty() {
                    continue;
                }

                // 5. Find Frame/Window/Dialog children from the cache (zero D-Bus calls)
                let mut found_window = false;
                for child_path in cache.children(cp_str) {
                    if let Some(ci) = cache.items.get(child_path) {
                        if matches!(ci.role, Role::Frame | Role::Window | Role::Dialog) {
                            // Only bounds need a D-Bus call (1 per window, typically 1-3)
                            let rect = self
                                .get_component_rect(&ci.object.name, &ci.object.path.to_string())
                                .await;
                            windows.push(WindowInfo {
                                pid,
                                hwnd: 0,
                                title: ci.name.clone(),
                                exe_name: app_name.clone(),
                                rect,
                                visible: true,
                            });
                            found_window = true;
                        }
                    }
                }

                // 6. Fallback: if no Frame/Window child found, register the app itself
                if !found_window && pid > 0 {
                    windows.push(WindowInfo {
                        pid,
                        hwnd: 0,
                        title: app_name.clone(),
                        exe_name: app_name,
                        rect: Rect { x: 0, y: 0, width: 0, height: 0 },
                        visible: true,
                    });
                }
            }

            Ok(windows)
        })
    }

    fn get_ui_tree(&self, pid: u32) -> Result<UiElement> {
        self.block_on(async {
            let (bus, path) = self.find_app_root(pid).await?;
            self.build_element_async(&bus, &path, true, 0).await
        })
    }

    fn get_ui_tree_hwnd(&self, _hwnd: usize) -> Result<UiElement> {
        Err(anyhow!(
            "Linux does not use window handles (HWND). Use the PID-based endpoint instead."
        ))
    }

    fn find_elements(
        &self,
        pid: u32,
        query: Option<&str>,
        element_type: Option<&ElementType>,
        interactive_only: bool,
    ) -> Result<Vec<UiElement>> {
        self.block_on(async {
            let (bus, path) = self.find_app_root(pid).await?;
    
            let results = self
                .search_elements_cached(&bus, &path, query, element_type, interactive_only)
                .await
                .unwrap_or_else(|e| {
                    tracing::warn!("Cache search failed for PID {}: {}. Skipping.", pid, e);
                    vec![]
                });
    
            tracing::info!("Cache search returned {} results for PID {}", results.len(), pid);
            Ok(results)
        })
    }
    

    fn find_elements_hwnd(
        &self,
        _hwnd: usize,
        _query: Option<&str>,
        _element_type: Option<&ElementType>,
        _interactive_only: bool,
    ) -> Result<Vec<UiElement>> {
        Err(anyhow!(
            "Linux does not use window handles (HWND). Use the PID-based endpoint instead."
        ))
    }

    fn click_element(&self, oculos_id: &str) -> Result<()> {
        let (bname, opath) = self.get_stored(oculos_id)?;
        self.block_on(async {
            let ap = Self::make_action_proxy(&self.connection, &bname, &opath)
                .await
                .context("Element does not support Action interface")?;

            let action_list = ap.get_actions().await.unwrap_or_default();
            for (i, (name, _, _)) in action_list.iter().enumerate() {
                if matches!(name.as_str(), "click" | "press" | "activate") {
                    ap.do_action(i as i32).await?;
                    return Ok(());
                }
            }
            if !action_list.is_empty() {
                ap.do_action(0).await?;
                return Ok(());
            }
            Err(anyhow!(
                "No clickable action found on element '{}'",
                oculos_id
            ))
        })
    }

    fn set_text(&self, oculos_id: &str, text: &str) -> Result<()> {
        let (bname, opath) = self.get_stored(oculos_id)?;
        self.block_on(async {
            let ep = Self::make_editable_text_proxy(&self.connection, &bname, &opath)
                .await
                .context("Element does not support EditableText interface")?;
            let tp = Self::make_text_proxy(&self.connection, &bname, &opath)
                .await
                .context("Element does not support Text interface")?;

            let cc = tp.character_count().await.unwrap_or(0);
            if cc > 0 {
                let _ = ep.delete_text(0, cc).await;
            }
            ep.insert_text(0, text, text.len() as i32).await?;
            Ok(())
        })
    }

    fn send_keys(&self, oculos_id: &str, text: &str) -> Result<()> {
        self.focus_element(oculos_id)?;
        std::thread::sleep(std::time::Duration::from_millis(60));
        send_key_sequence_linux(text);
        Ok(())
    }

    fn focus_element(&self, oculos_id: &str) -> Result<()> {
        let (bname, opath) = self.get_stored(oculos_id)?;
        self.block_on(async {
            let cp = Self::make_component_proxy(&self.connection, &bname, &opath)
                .await
                .context("Element does not support Component interface")?;
            cp.grab_focus().await?;
            Ok(())
        })
    }

    fn toggle_element(&self, oculos_id: &str) -> Result<()> {
        self.click_element(oculos_id)
    }

    fn expand_element(&self, oculos_id: &str) -> Result<()> {
        let (bname, opath) = self.get_stored(oculos_id)?;
        self.block_on(async {
            let ap = Self::make_action_proxy(&self.connection, &bname, &opath)
                .await
                .context("Element does not support Action interface")?;
            let action_list = ap.get_actions().await.unwrap_or_default();
            for (i, (name, _, _)) in action_list.iter().enumerate() {
                if matches!(name.as_str(), "expand or contract" | "expand" | "open") {
                    ap.do_action(i as i32).await?;
                    return Ok(());
                }
            }
            Err(anyhow!("No expand action found on element '{}'", oculos_id))
        })
    }

    fn collapse_element(&self, oculos_id: &str) -> Result<()> {
        self.expand_element(oculos_id)
    }
    fn select_element(&self, oculos_id: &str) -> Result<()> {
        self.click_element(oculos_id)
    }

    fn set_range(&self, oculos_id: &str, value: f64) -> Result<()> {
        let (bname, opath) = self.get_stored(oculos_id)?;
        self.block_on(async {
            let vp = Self::make_value_proxy(&self.connection, &bname, &opath)
                .await
                .context("Element does not support Value interface")?;
            vp.set_current_value(value).await?;
            Ok(())
        })
    }

    fn scroll_element(&self, oculos_id: &str, direction: &str) -> Result<()> {
        let key = match direction {
            "up" => "Up",
            "down" => "Down",
            "left" => "Left",
            "right" => "Right",
            "page-up" => "Page_Up",
            "page-down" => "Page_Down",
            other => return Err(anyhow!("Unknown scroll direction '{}'", other)),
        };
        self.focus_element(oculos_id)?;
        std::thread::sleep(std::time::Duration::from_millis(30));
        send_key_sequence_linux(&format!("{{{}}}", key));
        Ok(())
    }

    fn scroll_into_view(&self, _oculos_id: &str) -> Result<()> {
        Err(anyhow!(
            "scroll-into-view is not natively supported on Linux AT-SPI2."
        ))
    }

    fn focus_window(&self, pid: u32) -> Result<()> {
        let output = std::process::Command::new("xdotool")
            .args([
                "search",
                "--pid",
                &pid.to_string(),
                "--onlyvisible",
                "windowactivate",
            ])
            .output();
        match output {
            Ok(o) if o.status.success() => Ok(()),
            _ => {
                let _ = std::process::Command::new("wmctrl")
                    .args(["-i", "-a", &format!("0x{:08x}", pid)])
                    .output();
                Ok(())
            }
        }
    }

    fn close_window(&self, pid: u32) -> Result<()> {
        let output = std::process::Command::new("xdotool")
            .args([
                "search",
                "--pid",
                &pid.to_string(),
                "--onlyvisible",
                "windowclose",
            ])
            .output();
        match output {
            Ok(o) if o.status.success() => Ok(()),
            _ => Err(anyhow!(
                "Failed to close window for PID {}. Is xdotool installed?",
                pid
            )),
        }
    }
}

// ── Registry helper ───────────────────────────────────────────────────────────

impl LinuxUiBackend {
    fn get_stored(&self, oculos_id: &str) -> Result<(String, String)> {
        let entry = self
            .registry
            .get(oculos_id)
            .ok_or_else(|| anyhow!("Element '{}' not found in registry", oculos_id))?;
        let bname = entry.value().bus_name.clone();
        let opath = entry.value().object_path.clone();
        drop(entry);
        Ok((bname, opath))
    }
}

// ── Linux keyboard simulation via xdotool ─────────────────────────────────────

fn send_key_sequence_linux(text: &str) {
    let mut chars = text.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '{' {
            let mut key_name = String::new();
            while let Some(&c) = chars.peek() {
                chars.next();
                if c == '}' {
                    break;
                }
                key_name.push(c);
            }
            send_special_key_linux(&key_name);
        } else {
            let _ = std::process::Command::new("xdotool")
                .args(["type", "--clearmodifiers", &ch.to_string()])
                .output();
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
    }
}

fn send_special_key_linux(key_name: &str) {
    let xdotool_key = match key_name {
        "ENTER" | "RETURN" => "Return",
        "TAB" => "Tab",
        "ESC" | "ESCAPE" => "Escape",
        "SPACE" => "space",
        "DELETE" => "Delete",
        "BACKSPACE" => "BackSpace",
        "UP" => "Up",
        "DOWN" => "Down",
        "LEFT" => "Left",
        "RIGHT" => "Right",
        "HOME" => "Home",
        "END" => "End",
        "PGUP" => "Page_Up",
        "PGDN" => "Page_Down",
        "F1" => "F1",
        "F2" => "F2",
        "F3" => "F3",
        "F4" => "F4",
        "F5" => "F5",
        "F6" => "F6",
        "F7" => "F7",
        "F8" => "F8",
        "F9" => "F9",
        "F10" => "F10",
        "F11" => "F11",
        "F12" => "F12",
        s if s.contains('+') => {
            let parts: Vec<&str> = s.splitn(2, '+').collect();
            let modifier = match parts[0] {
                "CTRL" => "ctrl",
                "ALT" => "alt",
                "SHIFT" => "shift",
                "WIN" | "SUPER" => "super",
                other => other,
            };
            let key = parts.get(1).unwrap_or(&"").to_lowercase();
            let combo = format!("{}+{}", modifier, key);
            let _ = std::process::Command::new("xdotool")
                .args(["key", "--clearmodifiers", &combo])
                .output();
            return;
        },
        _ => return,
    };

    let _ = std::process::Command::new("xdotool")
        .args(["key", "--clearmodifiers", xdotool_key])
        .output();
    std::thread::sleep(std::time::Duration::from_millis(20));
}
