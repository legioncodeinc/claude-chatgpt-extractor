# Search and read

## Writing a query

`sessions_search` matches words, not meaning. It looks in each message's text and in its conversation's title.

| You type | It matches |
| --- | --- |
| `webhook forwarding` | Messages where both words appear, in the text or the conversation title, in any order |
| `"payment failed"` | Messages containing that exact phrase |
| `stripe "payment failed"` | Messages matching both the word and the phrase |
| `webhook OR stripe` | Messages containing the three words `webhook`, `or`, `stripe`. There are no operators |

Practical habits:

- Start with two to four distinctive words: a product name, an error string, a file name, a function name.
- Matching ignores case and accents. It does not stem: `deploy` will not match `deployment`. Search the form the user would have typed.
- An error message or identifier in double quotes is usually the fastest route to the right session.
- If nothing matches, drop the least distinctive word and try again before changing strategy.

Because titles are searched, a hit's snippet sometimes does not contain your words: the conversation's title does. Several such hits from one `session_id` mean the whole conversation is about the topic. Read it from the start rather than around each hit.

## Narrowing

| Parameter | Use it when |
| --- | --- |
| `source` | The user names the tool ("in ChatGPT", "in Codex") |
| `project` | The user names a repository or folder. Part of the path is enough. This excludes chat exports, which have no project |
| `start_date`, `end_date` | The user gives a time frame. Convert "last week" to `YYYY-MM-DD` dates yourself. Both are inclusive and UTC |

Filters are combined with the query, so a filter that is slightly wrong hides everything. If a filtered search is empty, retry once without the filter.

## Reading the context

A search result is one message. The answer is usually in the messages around it.

```json
{ "session_id": "codex:0199a2b4-...", "position": 47, "role": "assistant", "timestamp": "2026-11-03T15:22:10+00:00" }
```

Call `sessions_read` with `session_id` and `offset` of about `position - 3`, and a `limit` of 10 to 15. Then:

- `next_offset` continues forward. Keep reading only while it is still on topic.
- If a message has `text_truncated: true`, call again with `offset=next_offset` and `text_offset=next_text_offset` to get the rest of that message.
- `done: true` means you reached the end of the conversation.
- `total` is the number of messages in the conversation, useful for judging how much is left.

## More than one page of results

`total` is the number of matching messages. When `has_more` is true, pass `next_offset` as `offset`. Several hits in the same `session_id` usually mean one conversation: read it once rather than once per hit.

## Answering

- Lead with the answer, then where it came from: source, session title, date, and `session_id`.
- Quote the decisive sentence when wording matters (a decision, a command, a number).
- If two conversations disagree, say so and give both dates. The later one is not automatically right.
- `[REDACTED]` in the text is a masked secret. Do not guess what it was, and do not ask the user to paste it.
- If nothing relevant turns up after a few reworded searches, say that plainly, list what you searched for, and mention that chats from the ChatGPT or Claude apps only appear after an export is imported.

## When the tool reports a problem

| What you see | What to do |
| --- | --- |
| `note`: the index is empty | Call `sessions_refresh`, then search again |
| `note`: the index is still building | Call `sessions_status`; search again once `indexing.state` is `complete` |
| `note`: no searchable words | The query was only punctuation. Use words |
| Error: `Session not found` | Use the `session_id` exactly as search returned it, with its prefix |
| Error: date must be `YYYY-MM-DD` | Convert the date and retry |
| Error: index is busy | Wait a few seconds and retry once |
