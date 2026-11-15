"""Write CutShare's Finder Quick Actions into ~/Library/Services.

Usage: python3 make_quick_actions.py /path/to/python3
Each Quick Action is an Automator "Run Shell Script" workflow that calls
`<python> -m cutshare ui <action> "$@"`, with the full python path baked in
because Finder runs Quick Actions without your Terminal PATH (conda, brew...).
"""
import os
import plistlib
import shutil
import sys
import uuid

ACTIONS = [
    # (menu title, ui action, works on)
    ("CutShare – New project from this export", "new", "files"),
    ("CutShare – Submit to segment", "submit", "files"),
    ("CutShare – Open a segment", "pull", "folders"),
    ("CutShare – Assemble master", "assemble", "folders"),
    ("CutShare – Status", "status", "folders"),
]

INPUT = {
    "files": ("com.apple.Automator.fileSystemObject", ["public.item"]),
    "folders": ("com.apple.Automator.fileSystemObject.folder", ["public.folder"]),
}


def script_for(runner, action):
    """runner is either the `cutshare` launcher or a python3 that has cutshare installed."""
    cmd = f'"{runner}" ui' if os.path.basename(runner) == "cutshare" else f'"{runner}" -m cutshare ui'
    return (
        "export LANG=en_US.UTF-8\n"
        'mkdir -p "$HOME/.cutshare"\n'
        f'{cmd} {action} "$@" >> "$HOME/.cutshare/quick-actions.log" 2>&1\n'
    )


def document(python, action, kind):
    type_id, _ = INPUT[kind]
    action_dict = {
        "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.string"]},
        "AMActionVersion": "2.0.3",
        "AMApplication": ["Automator"],
        "AMParameterProperties": {"COMMAND_STRING": {}, "CheckedForUserDefaultShell": {},
                                  "inputMethod": {}, "shell": {}, "source": {}},
        "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
        "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
        "ActionName": "Run Shell Script",
        "ActionParameters": {
            "COMMAND_STRING": script_for(python, action),
            "CheckedForUserDefaultShell": True,
            "inputMethod": 1,  # pass input as arguments ("$@")
            "shell": "/bin/bash",
            "source": "",
        },
        "BundleIdentifier": "com.apple.RunShellScript",
        "CFBundleVersion": "2.0.3",
        "CanShowSelectedItemsWhenRun": False,
        "CanShowWhenRun": True,
        "Category": ["AMCategoryUtilities"],
        "Class Name": "RunShellScriptAction",
        "InputUUID": str(uuid.uuid4()).upper(),
        "Keywords": ["Shell", "Script", "Command", "Run", "Unix"],
        "OutputUUID": str(uuid.uuid4()).upper(),
        "UUID": str(uuid.uuid4()).upper(),
        "UnlocalizedApplications": ["Automator"],
        "arguments": {},
        "isViewVisible": 1,
        "location": "309.000000:253.000000",
        "nibPath": "/System/Library/Automator/Run Shell Script.action/Contents/Resources/Base.lproj/main.nib",
    }
    return {
        "AMApplicationBuild": "534",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [{"action": action_dict, "isViewVisible": 1}],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleID": "com.apple.finder",
            "applicationBundleIDsByPath": {"/System/Library/CoreServices/Finder.app": "com.apple.finder"},
            "applicationPath": "/System/Library/CoreServices/Finder.app",
            "applicationPaths": ["/System/Library/CoreServices/Finder.app"],
            "inputTypeIdentifier": type_id,
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 15,
            "processesInput": False,
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier": type_id,
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": False,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": False,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }


def info(title, kind):
    _, types = INPUT[kind]
    return {
        "NSServices": [{
            "NSBackgroundColorName": "background",
            "NSIconName": "NSActionTemplate",
            "NSMenuItem": {"default": title},
            "NSMessage": "runWorkflowAsService",
            "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
            "NSSendFileTypes": types,
        }]
    }


def write_all(python, dest):
    os.makedirs(dest, exist_ok=True)
    made = []
    for title, action, kind in ACTIONS:
        bundle = os.path.join(dest, f"{title}.workflow")
        if os.path.exists(bundle):
            shutil.rmtree(bundle)
        os.makedirs(os.path.join(bundle, "Contents"))
        with open(os.path.join(bundle, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump(info(title, kind), f)
        with open(os.path.join(bundle, "Contents", "document.wflow"), "wb") as f:
            plistlib.dump(document(python, action, kind), f)
        made.append(bundle)
    return made


def menu_titles():
    return [title for title, _, _ in ACTIONS]


if __name__ == "__main__":
    if sys.argv[1:2] == ["--titles"]:
        print("\n".join(menu_titles()))
        sys.exit(0)
    py = sys.argv[1] if len(sys.argv) > 1 else sys.executable
    dest = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/Library/Services")
    for b in write_all(py, dest):
        print("  installed", os.path.basename(b))
