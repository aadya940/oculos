from setuptools import Distribution, setup


class BinaryDistribution(Distribution):
    """Mark wheels as platform-specific because orbit bundles native binaries."""

    def has_ext_modules(self):
        return True


setup(distclass=BinaryDistribution)
