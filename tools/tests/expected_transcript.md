# Payload samples: `chat` / `ada`

- Sample enters: `chat` (entry)
- Mode: replay (every source answered from its tape; nothing went to a live source)
- Bricks: 3
- Samples: at most three of each kind, `smallest`, `typical` (lower median) and `largest` by the size of their canonical JSON; a payload over 1,000 bytes is trimmed to its top-level keys.

## assistant (open)

### Run

1 call during this sample; 1 shown.

#### only: from chat

**input** (46 bytes)

```json
{
  "steps": [
    "ship-one",
    "ship-two"
  ],
  "user": "ada"
}
```

**output** (40 bytes)

```json
{
  "text": "hello ada, ship-one, ship-two"
}
```

### Joints

None declared.

### Ports

#### source `xai` (epoch 2, label model=grok-4, followable)

1 take on the tape; 1 shown.

##### only: `bricks/assistant/input/tapes/xai/reply.json`

**request** (73 bytes)

```json
{
  "label": {
    "model": "grok-4"
  },
  "steps": [
    "ship-one",
    "ship-two"
  ],
  "user": "ada"
}
```

**response** (40 bytes)

```json
{
  "text": "hello ada, ship-one, ship-two"
}
```

## chat (entry)

### Run

1 call during this sample; 1 shown.

#### only: from sample input

**input** (28 bytes)

```json
{
  "goal": "ship",
  "user": "ada"
}
```

**output** (86 bytes)

```json
{
  "greeting": "hello ada, ship-one, ship-two",
  "steps": [
    "ship-one",
    "ship-two"
  ],
  "turns": 3
}
```

### Joints

#### to `assistant` (orchestrated, pin v2, follows xai)

1 crossing; 1 shown.

##### only

**crossed** (46 bytes)

```json
{
  "steps": [
    "ship-one",
    "ship-two"
  ],
  "user": "ada"
}
```

**came back** (40 bytes)

```json
{
  "text": "hello ada, ship-one, ship-two"
}
```

#### to `planner` (eventual, pin v1)

1 crossing; 1 shown.

##### only

**crossed** (15 bytes)

```json
{
  "goal": "ship"
}
```

**came back** (33 bytes)

```json
{
  "steps": [
    "ship-one",
    "ship-two"
  ]
}
```

### Ports

#### source `history` (epoch 1, label store=v1)

1 take on the tape; 1 shown.

##### only: `bricks/chat/input/tapes/history/turns.json`

**request** (37 bytes)

```json
{
  "label": {
    "store": "v1"
  },
  "user": "ada"
}
```

**response** (11 bytes)

```json
{
  "turns": 3
}
```

## planner (sealed)

### Run

1 call during this sample; 1 shown.

#### only: from chat

**input** (15 bytes)

```json
{
  "goal": "ship"
}
```

**output** (33 bytes)

```json
{
  "steps": [
    "ship-one",
    "ship-two"
  ]
}
```

### Joints

None declared.

### Ports

None declared.
