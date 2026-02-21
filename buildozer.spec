[app]

# (str) Title of your application
title = CopterSonde GCS

# (str) Package name
package.name = coptersondeGCS

# (str) Package domain (needed for android/ios packaging)
package.domain = com.intermetsystems

# (str) Source code where the main.py lives
source.dir = .

# (list) Source files to include (let empty to include all the files)
source.include_exts = py,png,jpg,kv,atlas

# (list) Source directories to exclude from the APK
source.exclude_dirs = docs,scripts,p4a-recipes,.git,__pycache__,bin,logs

# (str) Application entry point – Buildozer looks for main.py in source.dir
# We use a thin wrapper that bootstraps the real app from app/main.py.
# The actual entry point is main.py in the repo root (created by build step).

# (str) Application versioning
version = 0.1.0

# (list) Application requirements
requirements = python3,kivy==2.3.1,pymavlink,certifi

# (str) Supported orientation (landscape, portrait, or all)
orientation = landscape

# (bool) Indicate if the application should be fullscreen or not
fullscreen = 1

# (list) Permissions
android.permissions = INTERNET,ACCESS_NETWORK_STATE,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE

# (int) Target Android API
android.api = 33

# (int) Minimum API your APK will support
android.minapi = 24

# (str) Android NDK version to use
android.ndk = 25b

# (int) Android SDK version to use (leave commented for buildozer default)
# android.sdk = 33

# (str) python-for-android branch to use
p4a.branch = develop

# (str) Path to local python-for-android recipes (pymavlink needs a custom
# recipe to skip lxml/fastcrc C dependencies)
p4a.local_recipes = ./p4a-recipes

# (str) Bootstrap to use for the android application
p4a.bootstrap = sdl2

# (str) The Android arch to build for
android.archs = arm64-v8a

# (bool) Accept Android SDK license automatically
android.accept_sdk_license = True

# (bool) Use a virtual keyboard on Android
android.keyboard = True

# (str) Presplash background color (RRGGBB hex)
android.presplash_color = #1E1E24

# (str) Android manifest application theme
# android.manifest.theme = @android:style/Theme.NoTitleBar

# (list) Java .jar files to add (if any)
# android.add_jars =

# (list) Android additional libraries to copy into libs/armeabi
# android.add_libs_armeabi =

# (str) Android logcat filters to use
android.logcat_filters = *:S python:D

# (bool) Android logcat only display log for the app's PID
android.logcat_pid_only = True

# (str) Android entry point (leave default)
# android.entrypoint =

# (bool) Skip byte compile for .py files
# android.no-byte-compile-python = False

# ---------------------------------------------------------------------------
# Buildozer general settings
# ---------------------------------------------------------------------------

[buildozer]

# (int) Log level (0 = error only, 1 = info, 2 = debug (with command output))
log_level = 2

# (int) Display warning if buildozer is run as root (0 = False, 1 = True)
warn_on_root = 1
