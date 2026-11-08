# Controlling automatic side-panel opening

Side elements open the panel by default. Set `auto_expand=False` on an element
when it should stay available through its message reference without reopening a
panel the user has closed:

```python
sources = cl.Text(
    name="Sources",
    content="Reference material",
    display="side",
    auto_expand=False,
)
await cl.Message(content="See Sources for details.", elements=[sources]).send()
```

The option works on every element type, including `CustomElement`. An already
open panel still receives content updates. Clicking an element reference opens
it regardless of `auto_expand`; inline and page display modes are unaffected.

`auto_expand` is a live presentation hint carried as the optional `autoExpand`
field in the element event. It is not persisted, so it needs no database
migration and does not change how restored historical threads are displayed.
Apply it to each new or updated element whose arrival should not open the panel;
a batch containing a changed element with the default `auto_expand=True` still
opens it.

Older clients ignore the unknown `autoExpand` field and keep their previous
behavior.

## Opening the side panel from the backend

Use `cl.open_side_view(element_id)` to open an element that the client already
received for the current thread. It sends only the element id, so the element
payload is not sent again:

```python
@cl.on_message
async def main(message: cl.Message):
    details = cl.Text(
        name="Details",
        content="Reference material",
        display="side",
        auto_expand=False,
    )
    await cl.Message(content="See Details.", elements=[details]).send()
    await cl.open_side_view(details.id)
```

The client resolves the id against the elements it holds. An unknown id is a
silent no-op (debug log only), so elements must be sent before they can be
opened. Older clients ignore the `open_side_view` event.
