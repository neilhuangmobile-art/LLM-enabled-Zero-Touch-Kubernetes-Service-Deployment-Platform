# Translation Layer (Gemini Normalization) — What the Messages Mean

The translation layer is an optional pre-processing step (`USE_LLM_NORMALIZE=1`) that runs
before the local model. It uses Gemini to rewrite a vague or colloquial user request into a
clear, canonical sentence, then hands that sentence back to the local deterministic parser /
LoRA model to actually decide the deployment fields. The translation layer only rewrites — it
never invents a pod count, image, port, or memory value the user didn't state.

This system is currently disabled by default in `.env` (`USE_LLM_NORMALIZE=0`) because of the
known limitations below. Enable it only when you understand these behaviors.

## What you'll see in the server logs, and what it means

**`[Gemini 翻譯層] '<original>' → '<normalized>'`**
Normal, expected behavior. The translation layer rewrote the input and the rewritten text is
what actually goes to the local parser/model. If the deployment result looks wrong, compare the
`<normalized>` text against what you actually typed — a mismatch here usually means the
translation layer misunderstood the request, not that the local model is broken.

**`[Gemini 翻譯層] 正規化失敗：<error>`**
The Gemini call failed for any reason (network, invalid key, rate limit, etc). The system falls
back to the original, unmodified input and continues normally through the local parser/model.
This is a safe failure — no request is ever blocked or lost because of a translation-layer
error, it just means that particular request didn't get the "clean up ambiguous phrasing"
benefit.

**`RESOURCE_EXHAUSTED` / `429` in the error text**
The Gemini free tier has two separate caps: 5 requests per minute, and 20 requests per day.
Hitting either one produces this error. It is expected behavior on the free tier under normal
testing volume, not a bug. The request still completes via fallback (see above) — nothing is
lost, but that request runs without translation-layer help.

**`被 max_output_tokens 截斷，捨棄結果改用原始輸入`**
The Gemini model spent its token budget on internal "thinking" tokens and didn't have room left
to finish the rewritten sentence, so the (incomplete) result was discarded and the original
input is used instead. If you see this often, it means the configured token budget is too small
for the current model version — this was fixed once already (raised from 150 to 1024 tokens on
2026-08-07) but could recur if the model changes its thinking-token behavior in a future update.

**Trailing punctuation in the normalized sentence**
Earlier versions of this system had a bug where Gemini would end the normalized sentence with a
period (e.g. `deploy 2 pods of redis.`), and the local parser's image-detection regex couldn't
match the image name because of the trailing punctuation, silently falling back to the default
image `nginx:latest` instead of the one the user actually asked for. This was fixed on
2026-08-07 by stripping trailing punctuation from the Gemini output and making the parser regex
tolerant of it. If you ever see a deployment's image field not matching what you typed while the
translation layer is on, this class of bug is the first thing to check.

## Why the parsed result is always shown before a real deployment happens

Regardless of whether the translation layer changed your wording, the Deploy Console always
shows the fully parsed result (app name, image, pod count, port) and the security/cost/perf
agent review before you confirm. This is not specific to the translation layer — it is the
existing two-step deploy flow (parse-and-preview, then confirm) — but it also acts as a safety
net if the translation layer ever misunderstands a request: check the shown `image` field before
confirming, especially right after enabling `USE_LLM_NORMALIZE=1`.
