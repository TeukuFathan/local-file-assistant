"""Conversational desktop entry point and public UI imports."""

from .controller import ConversationController, ReviewCard
from .desktop import DesktopApp
from .formatting import format_report, plan_summary, recovery_summary
from .services import Services
from .worker import SingleWorker

__all__ = [
    "ConversationController", "DesktopApp", "ReviewCard", "Services",
    "SingleWorker", "format_report", "main", "plan_summary", "recovery_summary",
]


def main():
    import tkinter as tk

    window = tk.Tk()
    DesktopApp(window, ConversationController(Services(), SingleWorker()))
    window.mainloop()
