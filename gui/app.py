import tkinter as tk
from tkinter import ttk

from .tab_campaign import CampaignTab
from .tab_editor import EditorTab
from .tab_config import ConfigTab
from .tab_files import FilesTab
from .tab_logo import LogoTab
from .tab_redirect import RedirectTab
from .tab_logs import LogsTab


class MailerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Mass Mailer Control Panel")
        self.geometry("1100x700")
        self.minsize(900, 550)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=5, pady=5)

        nb.add(CampaignTab(nb), text="  Campaign  ")
        nb.add(EditorTab(nb), text="  HTML Editor  ")
        nb.add(FilesTab(nb), text="  Files  ")
        nb.add(LogoTab(nb), text="  Logos  ")
        nb.add(RedirectTab(nb), text="  Redirects  ")
        nb.add(ConfigTab(nb), text="  Config  ")
        nb.add(LogsTab(nb), text="  Logs  ")


def main():
    app = MailerApp()
    app.mainloop()
