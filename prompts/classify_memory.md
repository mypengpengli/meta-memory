Classify exactly one candidate memory unit. Return JSON only:

```json
{"kind":"profile|state|event|relationship|goal|domain|session|candidate","domain":"work|learning|health|finance|relationships|daily-life|general","confidence":0.0,"reason":"short explanation"}
```

Do not convert an assistant opinion into a user fact. Prefer `candidate` when the
statement is uncertain, temporary, sensitive, or lacks source support.
