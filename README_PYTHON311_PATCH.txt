# Python 3.11 Railway patch

Add these files to the root of your GitHub repo:

.python-version
runtime.txt
nixpacks.toml

This forces Railway/Nixpacks to use Python 3.11 instead of trying Python 3.13.14.

After upload:
1. Commit changes
2. Railway -> Redeploy

If Railway has a variable named NIXPACKS_PYTHON_VERSION, set it to:
3.11
or delete it if it points to 3.13.
