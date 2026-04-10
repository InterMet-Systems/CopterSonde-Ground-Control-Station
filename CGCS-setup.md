
## Contents 
Detailed instructions for setting up a development environment on Windows 11, using PyCharm. 

For a brief version, see BUILD.md.

### Prerequisites

- Python 3.10, 3.11, or 3.12
- Git for Windows
- PyCharm (Community edition is fine)

### 1. Clone the Repository
Open a command prompt and navigate to the desired directory. This is the default PyCharm project directory, and the one that this guide will use. You can use anything you like, just substitute accordingly. 

	C:\Users\<username>\PycharmProjects\
You can enter this to step down the directory tree	

    cd <relative_path>

Or this to step up

    cd ..
When you have navigated to the desired directory, clone the CGCS repository by entering

    git clone https://github.com/InterMet-Systems/CopterSonde-Ground-Control-Station.git
This will create your local repository at

    C:\Users\<username>\PycharmProjects\CopterSonde-Ground-Control-Station
Your repository should be tracking the remote repository - verify this by entering

    git remote -v
It should return

    origin https://github.com/InterMet-Systems/CopterSonde-Ground-Control-Station.git (fetch)
    origin https://github.com/InterMet-Systems/CopterSonde-Ground-Control-Station.git (push)
If it does not, delete the folder and start from the beginning, taking care to enter (Or copy and paste) or URL correctly.
### 2. Create PyCharm Project
Run PyCharm, and it will show the welcome screen, with **New Script**, **New Project**, **Open**, and **Clone Repository** buttons along the top. It may also re-open the last project you were working on. If it does, ****File** > New Project** and **File > Open** are equivalent to the **New Project** and **Open** buttons.

PyCharm is smart, and automatically does many things correctly. There are easier ways to set the project up that will probably work, but the method used here allows for a greater degree of awareness of the different setups steps taken, and makes errors visible sooner, simplifying debugging.

Open the folder you just cloned, either via the **Open** button from the welcome screen, or **File > Open**. A dialog will appear titled **Creating virtual environment** with three fields: **Location**, **Base interpreter**, and **Dependencies**. Click **Cancel**, we will set up the virtual environment manually.

### 3. Set up the Virtual Environment
You may have multiple python versions installed, but the python PATH variable only points to one of them, and this project requires a specific version, so enter this to see what versions are available:

    py --list
This should return at least one python version between 3.10 and 3.12, for example:
 

    -V:3.14 *        Python 3.14 (64-bit)
    -V:3.12          Python 3.12 (64-bit)
    -V:3.10          Python 3.10 (64-bit)
 This indicates that three versions are available, and 3.14 is the one on the PATH. If you do not see one between 3.10 and 3.12, download and install "Windows installer (64-bit)" from this URL:

    https://www.python.org/downloads/release/python-31210/
If you installed python just now, because a compatible version was not listed, you will need to close and re-open the command prompt.

From the repository you just created, create the virtual environment, using the right python version. The prompt should be:

    C:\Users\<username>\PycharmProjects\CopterSonde-Ground-Control-Station
And the command for creating the virtual environment is:

    py -3.12 -m venv .venv
### 4. Configure the PyCharm interpreter
In PyCharm, go to **File > Settings > Project: <project_name> > Python Interpreter > Add Interpreter > Add Local Interpreter**. Select the **Existing** radio button.

In the **Python path** dropdown, select the virtual environment you just created. It will be:

    <desired-directory>\.venv\Scripts\python.exe

Click **Ok**, **Ok**.
### 5. Install Dependencies
Back to the command prompt, which should be in the repository, i.e.,

    C:\Users\<username>\PycharmProjects\CopterSonde-Ground-Control-Station
You will need to activate the virtual environment by entering:

    .venv\Scripts\activate
The prompt should change from "C:\..." to "(.venv) C:\...", confirming the virtual environment is active.

Now is a good time to go ahead and confirm that the python version in this virtual environment is correct, so enter:

    python --version
It should return the version that you chose earlier, when setting up the virtual environment. If it doesn't, delete the ".venv" folder in the repo, and do step 3 again. 

Upgrade pip, by entering:

    pip install --upgrade pip
 And install the dependencies by entering:
 

    pip install -r requirements.txt
 This should complete without errors. You should see "Successfully
installed kivy pymavlink etc..." If it does not, TODO: add "if it does not" for this, and the previous two commands

### 6. Install Custom PyMAVLink Dialect
CGCS uses custom MAVLink message types that are not included in the standard PyMAVLink package, And you need to build PyMAVLink from a custom definitions repository. In the command prompt, step up one level from the CGCS repository, I.E.,

    C:\Users\<username>\PycharmProjects

**Important** - The virtual environment you created earlier needs to be activate for this step. If it is not, all of the steps will complete normally, but you'll be installing pymavlink to your system, and not to the virtual environment.


    
And clone the custom PyMAVLink repository by entering:

    git clone -b BLISS-ARRC-main https://github.com/tony2157/my-mavlink.git
Step into the new repository by entering:

    cd my-mavlink
Update the submodules by entering:

    git submodule update --init pymavlink
Step into the "pymavlink" folder by entering:

    cd pymavlink
Create a temporary environment variable that pymavlink's build script uses to find the custom message definition XML files by entering:

    set MDEF=..\message_definitions
This environment variable lasts only for the command prompt's current session - it is automatically cleaned up when you close the command prompt.

Install the custom PyMAVLink (Overwriting the PyPI version) by entering:

    pip install .
The last line in the return should be "Successfully installed pymavlink...". If it is not, check this directory:

    C:\Users\<username>\PycharmProjects\CopterSonde-Ground-Control-Station\.venv\Lib\site-packages\pymavlink

If it doesn't exist, then pymavlink isn't installed in the virtual environment. Check this directory:

    C:\Users\<username>\AppData\Local\Programs\Python\Python312\Lib\site-packages\pymavlink

If it does exist, then you installed pymavlink to your system, which isn't really a problem on it's own. Just start step 6 from the top, and make sure the prompt starts with "(.venv) C:...". If it doesn't activate it like you did in step 5.


### 7. Configure PyCharm Project Structure
You need to mark the project root directory as a source. In PyCharm, go to **File > Settings > Project: <project_name> > Project Structure**. Right-click the project root directory, which should be:

    <desired-directory>\CopterSonde-Ground-Control-Station

Select **Sources** (Should now have a check mark next to it), and click **Ok**. This is necessary for imports such as "from app.main import main" resolve correctly.
### 8. Create a Run Configuration
In PyCharm, go to **Run > Edit Configurations**. Click the **+** button and select **Python**.

There are three things that you need to set.

First, there's a dropdown with options "**script**", and "**module**" - make sure it's set to "**script**".

Second, the entry point needs to be set. To the right of the aforementioned dropdown there is a long field - click the "browse" button. Select the repository root, app, and then "main.py". If you used the defaults, the directory will be:

    C:\Users\user\PycharmProjects\CopterSonde-Ground-Control-Station\app\main.py
Third, you need to select the interpreter. Directly below the "**Run**" label, there is a long field that needs to point to the python executable inside of the .venv folder you created earlier. It should automatically find it - so just confirm that it points to the right executable. If you used the defaults, the directory will be:

    C:\Users\user\PycharmProjects\CopterSonde-Ground-Control-Station\.venv\Scripts\python.exe
PyCharm attempts to detect the virtual environment's version, and will display it as:

 `"Python 3.12 (CopterSonde-Ground-Control-Station"`
 
But sometimes it gets the version is wrong, and I don't know why. As long as you followed step 3, and verified that it's right, this is only a cosmetic PyCharm bug. Just make sure the directory is correct.

You may also fill in the **Name**: field.

With that done, click **Ok**.
### 8. Run

Press the green triangle button, or shift+F10 to run the current configuration.

After launching the application, you should see the CopterSonde GCS window with a bottom navigation bar and the Connection screen displayed. To test without a real vehicle, toggle Demo Mode on the Connection screen. This activates simulated telemetry that populates all screens — you can switch between Flight, Map, Sensors, Profiles, Params, and Settings to verify everything is working.