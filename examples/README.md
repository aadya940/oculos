# Orbit Examples

End-to-end examples showing Orbit's composable verb pattern on real-world tasks.

## LinkedIn Easy Apply

[`linkedin_easy_apply.py`](linkedin_easy_apply.py) — Automatically applies to jobs on LinkedIn using Easy Apply.

### Usage

```bash
# Basic — apply to 10 Software Engineer Intern jobs
python linkedin_easy_apply.py -q "Software Engineer Intern" -n 10 -r ~/Desktop/RESUME.pdf

# Custom applicant profile from a file
python linkedin_easy_apply.py -q "ML Engineer" -n 5 -r resume.pdf -a profile.txt

# Use a different model
python linkedin_easy_apply.py -q "Data Scientist" -n 3 -r resume.pdf --llm gemini-2.5-pro
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `-q`, `--query` | Job search query | *(required)* |
| `-n`, `--count` | Number of applications to submit | `10` |
| `-r`, `--resume` | Path to resume PDF | *(required)* |
| `-a`, `--applicant` | Path to applicant info text file | Built-in defaults |
| `--llm` | LLM model to use | `gemini-3-flash-preview` |

### Applicant profile format

If using `--applicant`, the file should be plain text with one field per line:

```
- Phone: 4083906345
- City: San Jose, CA
- GPA: 3.8
- Graduation year: 2025
- Degree: Bachelor's in Computer Science
- University: San Jose State University
- Years of experience: 1
- Work authorization: Yes, authorized to work in the US
- Require sponsorship: No
- For any yes/no question about qualifications: Yes
```

If omitted, edit `DEFAULT_APPLICANT_INFO` in the script.

### Assumptions

- **You are logged into LinkedIn** in the default browser before running.
- **Resume is a PDF file** accessible at the path you provide.
- **Orbit is installed**
- **API key** is set for your chosen provider (`GEMINI_API_KEY`, `OPENAI_API_KEY`, etc.) or via `.env`.

### How it works
The script uses Orbit's verb pattern, each step is a short-horizon agent task, Python drives the state machine between them:
1. **Navigate** once to the LinkedIn search page
2. **Do** — click an unapplied job title
3. **Read** (typed) — extract `JobPanelState` from the right panel
4. **Do** — click Easy Apply
5. **Read** (typed) — extract `WizardPageState` (buttons, unfilled fields)
6. **Do** — fill empty fields using applicant info
7. **Do** — click Next / Review / Submit
8. Repeat 5-7 until submitted, then loop back to step 2

No re-navigation between jobs, one session handles the entire run.

### Note
This tool is not meant for low-latency applications, it is meant to be started and come after an hour with 20
job applications done with Intent. If it fails on any one application in-between, it skips.
