import PyInstaller.__main__
import os
import sys

# Define the entry point
entry_point = os.path.join('app.py')

# Define paths for assets and templates
templates_path = os.path.join('templates')
assets_path = 'assets'
icon_path = os.path.join(assets_path, 'ditto-x.ico')

# PyInstaller arguments
args = [
    entry_point,
    '--onefile',            # Package into a single executable
    '--windowed',           # No console window
    f'--icon={icon_path}',  # Set the icon
    '--name=DittoX',   # Name of the executable
    # Add templates and assets folder
    f'--add-data={templates_path};templates',
    f'--add-data={assets_path};assets',
    # Exclude unnecessary modules to reduce size
    '--exclude-module=tkinter',
    '--exclude-module=unittest',
    '--exclude-module=pydoc',
    '--exclude-module=pdb',
    '--exclude-module=IPython',
    '--clean',              # Clean PyInstaller cache before building
]

if __name__ == '__main__':
    print(f"Building executable from {entry_point}...")
    PyInstaller.__main__.run(args)
    print("\nBuild completed! The executable can be found in the 'dist' folder.")
