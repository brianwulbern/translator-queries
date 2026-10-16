# Placeholders filled inside a shared component

Some keys are handed to a shared component as a label, and the component fills
the placeholder itself, inside its own package. The product source only shows
the key being passed along, so the value cannot be read from it. The values
below come from the component's own source, written down once so the question
never has to be researched twice.

## Pager (`@acme/ui-pager`)

Every value is a plain number, and each label is read by a screen reader, not
drawn.

| Key | Arguments |
| --- | --- |
| `go-to-page-x` | `{0}` = the number of that page, for example `3` |
| `go-to-the-next-page-x` | `{0}` = the number of the next page, one more than the current page |
| `go-to-the-previous-page-x` | `{0}` = the number of the previous page, one less than the current page |
