"""Finder layout for the macOS Desktop disk image."""

app_path = defines["app"]
user_installer_path = defines["user_installer"]

format = "UDZO"
filesystem = "HFS+"
files = [app_path, (user_installer_path, "Install for This User.command")]
symlinks = {"Applications": "/Applications"}

background = "builtin-arrow"
window_rect = ((120, 120), (660, 390))
show_toolbar = False
show_sidebar = False
show_status_bar = False
default_view = "icon-view"
icon_size = 96
text_size = 12
icon_locations = {
    "Covalent Desktop.app": (165, 160),
    "Applications": (495, 160),
    "Install for This User.command": (330, 300),
}
hide_extensions = ["Covalent Desktop.app", "Install for This User.command"]
