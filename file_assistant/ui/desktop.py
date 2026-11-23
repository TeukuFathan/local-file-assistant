"""Tkinter widgets and rendering for the conversational desktop."""

from pathlib import Path

from .formatting import plan_summary, recovery_summary
from .theme import COLORS, RoundedPanel, apply_theme


class DesktopApp:
    """Tk widgets render only controller-owned state; all text stays inert."""

    def __init__(self, window, controller):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.window, self.c = window, controller
        self._scroll_job = None
        self.font_family = apply_theme(window)
        window.title("Local File Assistant")
        window.geometry("1080x850")
        window.minsize(820, 650)
        brand = tk.Frame(window, background=COLORS["background"])
        brand.pack(fill="x", padx=30, pady=(24, 18))
        tk.Label(brand, text="Local File Assistant", background=COLORS["background"],
                 foreground=COLORS["text"], font=(self.font_family, 23, "bold")).pack(anchor="w")
        tk.Label(brand, text="Organize by filename. You stay in control.", background=COLORS["background"],
                 foreground=COLORS["muted"], font=(self.font_family, 11)).pack(anchor="w", pady=(5, 0))
        context = RoundedPanel(window)
        context.pack(fill="x", padx=28)
        header = tk.Frame(context.content, background=COLORS["surface"])
        header.pack(fill="x")
        self.folder = ttk.Label(header, text="No folder selected", wraplength=690)
        self.folder.pack(side="left", fill="x", expand=True)
        self.choose = ttk.Button(header, text="Change folder", command=self._choose, style="Quiet.TButton")
        self.choose.pack(side="right")
        controls = ttk.Frame(context.content)
        controls.pack(fill="x", pady=(14, 0))
        ttk.Label(controls, text="Local model", foreground=COLORS["muted"]).pack(side="left", padx=(0, 10))
        field = RoundedPanel(controls, padding=4, backdrop=COLORS["surface"], width=210)
        field.pack(side="left", padx=(0, 8))
        self.model = ttk.Entry(field.content, width=22, style="Flat.TEntry")
        self.model.pack(fill="x")
        self.model_button = ttk.Button(controls, text="Select model", command=self._model, style="Soft.TButton")
        self.model_button.pack(side="left")
        self.scan = ttk.Button(controls, text="Inspect / rescan", command=controller.rescan, style="Quiet.TButton")
        self.scan.pack(side="left", padx=8)
        self.stop = ttk.Button(controls, text="Stop", command=controller.cancel, style="Quiet.TButton")
        self.stop.pack(side="right")
        self.status = tk.Label(window, background=COLORS["background"], foreground=COLORS["muted"],
                               anchor="w", wraplength=980, font=(self.font_family, 10))
        self.status.pack(fill="x", padx=34, pady=(12, 8))
        self.recovery_button = ttk.Button(window, text="Review recovery evidence",
                                         command=controller.open_recovery_confirmation, style="Soft.TButton")
        conversation = tk.Frame(window, background=COLORS["background"])
        conversation.pack(fill="both", expand=True, padx=(28, 18))
        self.conversation = conversation
        self.canvas = tk.Canvas(conversation, highlightthickness=0, background=COLORS["background"])
        scrollbar = ttk.Scrollbar(conversation, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True, padx=(0, 8))
        self.body = tk.Frame(self.canvas, background=COLORS["background"])
        self.body_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.body.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.body_id, width=event.width))
        composer_panel = RoundedPanel(window, padding=14)
        composer_panel.pack(fill="x", padx=28, pady=(12, 8))
        composer = composer_panel.content
        self.input = tk.Text(composer, height=2, wrap="word", font=(self.font_family, 12),
                             relief="flat", borderwidth=0, highlightthickness=0,
                             background=COLORS["surface"], foreground=COLORS["text"],
                             insertbackground=COLORS["accent"], padx=8, pady=7)
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", self._send_enter)
        self.send_button = ttk.Button(composer, text="Send", command=self._send, style="Primary.TButton")
        self.send_button.pack(side="right", padx=(12, 0))
        tk.Label(window, text="Filenames only  ·  Enter to send  ·  Nothing moves without confirmation",
                 background=COLORS["background"], foreground=COLORS["muted"],
                 font=(self.font_family, 9)).pack(pady=(0, 16))
        self.c.on_change = self.render
        self.window.protocol("WM_DELETE_WINDOW", self.c.close)
        self.render()
        self.window.after(50, self._poll)

    def _choose(self):
        from tkinter import filedialog
        picked = filedialog.askdirectory(parent=self.window, title="Choose the folder to organize", mustexist=True)
        if picked:
            self.c.select_root(Path(picked))

    def _model(self):
        self.c.select_model(self.model.get())

    def _send_enter(self, event):
        self._send()
        return "break"  # Enter can send text, never confirms operations.

    def _send(self):
        if self.c.send(self.input.get("1.0", "end-1c")):
            self.input.delete("1.0", "end")

    def _text(self, parent, text, *, height=None):
        # Text widgets permit selecting/copying full long paths. No markup,
        # elision, file opening, links, or interpretation of model text.
        surface = parent.cget("background")
        frame = self.tk.Frame(parent, background=surface)
        frame.pack(fill="x", pady=(7, 3))
        widget = self.tk.Text(frame, wrap="word", width=1,
                              height=height or min(12, max(1, len(text) // 90 + text.count("\n") + 1)),
                              font=(self.font_family, 11), background=surface, foreground=COLORS["text"],
                              relief="flat", borderwidth=0, highlightthickness=0,
                              selectbackground="#DCD9FA", selectforeground=COLORS["text"],
                              spacing1=2, spacing3=3, padx=0, pady=0)
        scrollbar = self.ttk.Scrollbar(frame, orient="vertical", command=widget.yview)

        def scroll(first, last):
            scrollbar.set(first, last)
            if float(first) <= 0 and float(last) >= 1:
                scrollbar.pack_forget()
            elif not scrollbar.winfo_manager():
                scrollbar.pack(side="right", fill="y", before=widget)

        widget.configure(yscrollcommand=scroll)
        widget.insert("1.0", text)
        widget.configure(state="disabled")
        widget.pack(side="left", fill="x", expand=True)
        if height is None:
            def fit(event):
                count = widget.count("1.0", "end-1c", "displaylines")
                wanted = min(12, max(1, (count[0] if count else 0) + 1))
                if int(widget.cget("height")) != wanted:
                    widget.configure(height=wanted)
            widget.bind("<Configure>", fit)
        return widget

    def _panel(self, title, *, surface=None):
        panel = RoundedPanel(self.body, surface=surface)
        panel.pack(fill="x", pady=8, padx=2)
        self.tk.Label(panel.content, text=title, background=panel.surface,
                       foreground=COLORS["accent"], font=(self.font_family, 10, "bold"),
                       anchor="w").pack(fill="x", pady=(0, 3))
        return panel.content

    def _card(self, card):
        ttk, tk, c = self.ttk, self.tk, self.c
        frame = self._panel(f"MOVE REVIEW  ·  {card.status}")
        active = (card.status == "Current" and c.plan is card.plan
                  and not c.busy and not c.closing and c.state in ("reviewable", "confirming"))
        self._text(frame, plan_summary(card.plan))
        by_id = {f.file_id: f.filename for f in c.snapshot.files} if active and c.snapshot else {}
        existing = {folder.casefold() for folder in c.snapshot.folders} if active and c.snapshot else set()
        selected_paths = {op.request.source: op.request.destination for op in card.plan.operations
                          if op.request.action == "move"}
        variables = {}
        for move in card.proposal["moves"]:
            identifier = move["file_id"]
            variable = tk.BooleanVar(value=identifier in card.selected)
            variables[identifier] = variable
            source = by_id.get(identifier)
            if source is None:
                # Historical cards retain full paths in their immutable plan.
                continue
            folder = move["destination_folder"]
            destination = selected_paths.get(source, f"{folder}/{source}")
            folder_status = "already exists" if folder.casefold() in existing else "new; created only if selected and confirmed"
            label = f"{source} -> {destination}\nFolder: {folder_status}"
            row = tk.Frame(frame, background=COLORS["surface"])
            row.pack(fill="x", pady=5)
            check = ttk.Checkbutton(row, variable=variable, command=lambda: c.select_moves(
                card.plan.plan_id, tuple(key for key, value in variables.items() if value.get())))
            check.pack(side="left")
            check.configure(state="normal" if active else "disabled")
            self._text(row, label, height=3)
        button = ttk.Button(frame, text="Review and apply", command=lambda: c.open_confirmation(card.plan.plan_id),
                            style="Primary.TButton")
        button.pack(anchor="e", pady=(12, 0))
        button.configure(state="normal" if active and card.plan.operations and c.state == "reviewable" else "disabled")

    def render(self):
        c, ttk = self.c, self.ttk
        self.folder.configure(text=f"Folder: {c.root}" if c.root else "No folder selected")
        self.status.configure(text=f"{c.state.replace('_', ' ').capitalize()} | Model: {c.model or 'none selected'} | "
                              f"Ollama: {c.model_status}" + (" | Closing: waiting for worker outcome" if c.closing else ""))
        frozen = c.state == "applying" or c.closing
        self.choose.configure(state="disabled" if frozen else "normal")
        self.scan.configure(state="disabled" if frozen or c.root is None else "normal")
        self.model_button.configure(state="disabled" if frozen or c.busy else "normal")
        self.model.configure(state="disabled" if frozen or c.busy else "normal")
        self.send_button.configure(state="disabled" if c.busy or c.closing or c.recovery_blocked else "normal")
        self.stop.configure(state="normal" if c.busy else "disabled")
        if c.recovery_review:
            self.recovery_button.pack(fill="x", padx=30, pady=(0, 8), before=self.conversation)
            self.recovery_button.configure(state="disabled" if c.busy or c.closing else "normal")
        else:
            self.recovery_button.pack_forget()
        for child in self.body.winfo_children():
            child.destroy()
        for kind, value in c.timeline:
            if kind == "message":
                role, text = value
                frame = self._panel(role, surface=COLORS["tint"] if role == "You" else None)
                self._text(frame, text)
            else:
                self._card(value)
        if c.confirmation:
            plan = c.confirmation
            frame = self._panel("CONFIRM MOVES  ·  Exact selected plan")
            self._text(frame, plan_summary(plan))
            actions = ttk.Frame(frame)
            actions.pack(fill="x", pady=(14, 0))
            ttk.Button(actions, text="Confirm moves", command=lambda: c.confirm_moves(plan.plan_id),
                        style="Primary.TButton").pack(side="right")
            ttk.Button(actions, text="Cancel", command=c.cancel_confirmation,
                        style="Quiet.TButton").pack(side="right", padx=8)
        if c.recovery_confirmation:
            frame = self._panel("RECOVERY  ·  Separate acknowledgement")
            self._text(frame, recovery_summary(c.recovery_confirmation), height=14)
            review = c.recovery_confirmation
            actions = ttk.Frame(frame)
            actions.pack(fill="x", pady=(14, 0))
            ttk.Button(actions, text="Confirm acknowledgement", command=lambda: c.confirm_recovery(review),
                        style="Primary.TButton").pack(side="right")
            ttk.Button(actions, text="Cancel", command=c.cancel_recovery_confirmation,
                        style="Quiet.TButton").pack(side="right", padx=8)
        self._schedule_scroll()

    def _schedule_scroll(self):
        if self._scroll_job is not None:
            self.window.after_cancel(self._scroll_job)
            self._scroll_job = None
        if not self.c.closing:
            self._scroll_job = self.window.after_idle(self._scroll_to_end)

    def _scroll_to_end(self):
        self._scroll_job = None
        self.body.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(1.0)

    def _poll(self):
        self.c.worker.poll()
        if self.c.closed:
            if self._scroll_job is not None:
                self.window.after_cancel(self._scroll_job)
                self._scroll_job = None
            self.window.destroy()
        else:
            self.window.after(50, self._poll)
