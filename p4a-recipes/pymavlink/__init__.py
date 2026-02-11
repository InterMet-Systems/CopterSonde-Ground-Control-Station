"""python-for-android recipe for pymavlink with custom BLISS/ARRC messages.

pymavlink has no upstream p4a recipe.  Installing via pip would pull in
lxml and fastcrc (heavy C libraries) which are not needed at runtime.

This recipe:
  1. Downloads the standard pymavlink sdist from PyPI (runtime code).
  2. Downloads custom MAVLink XML definitions from the tony2157/my-mavlink
     fork (BLISS-ARRC-main branch) which adds CASS_SENSOR_RAW (msg 227)
     and ARRC_SENSOR_RAW (msg 228).
  3. Sets MDEF so pymavlink's setup.py regenerates the dialect modules
     from the custom XML — no lxml needed (mavgen uses stdlib
     xml.etree.ElementTree for parsing).
  4. Sets PYMAVLINK_FAST_INDEX=0 to skip the Cython/C dfindexer extension.
"""

from pythonforandroid.recipe import PythonRecipe
import os
import tarfile
import urllib.request


class PymavlinkRecipe(PythonRecipe):
    version = "2.4.49"
    url = (
        "https://files.pythonhosted.org/packages/source/p/"
        "pymavlink/pymavlink-{version}.tar.gz"
    )
    depends = ["python3", "setuptools"]
    site_packages_name = "pymavlink"
    call_hostpython_via_targetpython = False

    # Custom MAVLink definitions with CASS and ARRC messages
    _mavlink_repo_url = (
        "https://github.com/tony2157/my-mavlink/archive/"
        "refs/heads/BLISS-ARRC-main.tar.gz"
    )
    _mavlink_extract_dir = "my-mavlink-BLISS-ARRC-main"

    def get_recipe_env(self, arch):
        env = super().get_recipe_env(arch)
        # Skip the Cython dfindexer C extension
        env["PYMAVLINK_FAST_INDEX"] = "0"
        # Point setup.py at the custom message definitions so mavgen
        # regenerates dialect modules with CASS_SENSOR_RAW / ARRC_SENSOR_RAW
        env["MDEF"] = os.path.join(
            self.get_build_dir(arch.arch),
            self._mavlink_extract_dir,
            "message_definitions",
        )
        return env

    def prebuild_arch(self, arch):
        super().prebuild_arch(arch)
        build_dir = self.get_build_dir(arch.arch)
        defs_dir = os.path.join(build_dir, self._mavlink_extract_dir)

        if os.path.isdir(defs_dir):
            return  # already fetched on a previous run

        # Download and extract the custom MAVLink XML definitions
        archive = os.path.join(build_dir, "_mavlink-defs.tar.gz")
        urllib.request.urlretrieve(self._mavlink_repo_url, archive)
        with tarfile.open(archive) as tar:
            tar.extractall(build_dir)
        os.remove(archive)


recipe = PymavlinkRecipe()
