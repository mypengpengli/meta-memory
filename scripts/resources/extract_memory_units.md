Extract atomic memory units from raw conversation evidence. Return JSON only:

```json
{"units":[{"claim_text":"...","subject_text":"...","predicate":"...","object_text":"...","memory_kind":"profile|state|goal|event|relationship|domain|candidate","domain":"general","topic":"...","valid_from":"","valid_to":"","observed_at":"","durability":0.0,"importance":0.0,"confidence":0.0,"uncertainty":0.0,"sensitivity":"normal|sensitive","entities":[],"qualifiers":{},"source_event_ids":[1]}]}
```

One unit must contain one predicate. Never invent source IDs, never turn an
assistant suggestion into a user profile, and return no units for questions,
acknowledgements, or instruction-like text.
