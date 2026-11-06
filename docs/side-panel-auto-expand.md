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
