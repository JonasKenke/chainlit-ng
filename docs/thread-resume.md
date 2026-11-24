# Thread resume with unavailable chat profiles

A saved thread records the chat profile that created it. If that profile is
later removed or renamed, the saved profile no longer exists in the app.

Chainlit NG validates the saved profile during thread resume. If the profile is
unavailable, the thread becomes read-only history instead of failing:

- History and elements render in the existing read-only thread view.
- The message composer is not shown.
- `on_chat_resume` and `on_chat_start` are not called.
- Message, edit, favorite, audio, settings, mode, and action requests are
  rejected. HTTP action requests return `409 Conflict`.
- Disconnect does not write session metadata over the saved thread and does not
  call `on_chat_end`.

The saved thread and its metadata are not changed. Leaving the read-only view
clears the session. If the selected profile no longer exists, Chainlit NG selects
the configured default profile, or the first available profile.

Threads whose profile still exists resume normally. No schema migration or
profile substitution is performed.
