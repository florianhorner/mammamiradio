# TODOs

## P2: Narrow add-on detection
Current state: `_is_addon()` in `mammamiradio/config.py` treats `/data/options.json` as sufficient proof of add-on mode.

Why it matters: that can coerce a non-add-on environment onto `/data/go-librespot`, which recreates the same path-confusion class in a different form.

Where to start: tighten add-on detection so token-based Supervisor signals are primary, then add a regression test covering `/data/options.json` outside true add-on runtime.

## P2: Add runtime startup diagnosis
Current state: docs explain local vs add-on paths, but the runtime does not report resolved config dir, ownership mode, or why it attached vs spawned `go-librespot`.

Why it matters: when startup breaks, operators still have to infer state from scattered logs instead of getting one clear answer.

Where to start: add a small diagnostic surface from the launcher or app startup that prints the resolved config dir, active `go-librespot` PID ownership, and mismatch reasons.

## P2: Focus trap for setup gate modal overlay
The setup gate overlay does not trap keyboard focus. Tab key can reach elements behind the overlay, which breaks accessibility for keyboard and screen reader users. Standard modal pattern: trap focus inside the overlay while open, restore on close.
**Effort:** S (CC: ~5min) | **Depends on:** nothing | **Files:** mammamiradio/dashboard.html

## P3: Extract setup gate UI from dashboard.html
dashboard.html is now 1600+ lines with ~620 lines of inline setup gate CSS/JS/HTML. Extract into a separate template or at minimum a JS module. The file is the most-modified in the repo (28 touches in 30 days) and the monolith makes merge conflicts more likely.
**Effort:** S (CC: ~15min) | **Depends on:** nothing | **Files:** mammamiradio/dashboard.html, mammamiradio/streamer.py
