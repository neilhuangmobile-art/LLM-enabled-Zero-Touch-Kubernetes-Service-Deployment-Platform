"""使用者文字裡假冒的特殊標記，用零寬字元拆開（2026-11-18，主模型伺服器與多代理人流程共用）。

llama-server 與 HF tokenizer 預設都會把內容裡的 <|im_start|>、<|im_end|> 等字串轉成真正的特殊 token
（11-18 實測：一句含假冒 <|im_end|><|im_start|>system 的訊息讓角色標記從 5 個變 8 個），
等於讓使用者自己開一個 system 回合。不需要 tokenizer：Qwen 系列的特殊標記都是 <|...|> 或
<think>、<tool_call> 這類固定標籤。core/model_server._sanitize_user_text 另外再用 tokenizer 的清單補一層。
"""
import re

_SPECIAL_RE = re.compile(r"<(\|[^<>\s|]{1,40}\|)>|<(/?(?:think|tool_call|tool_response))>")


def sanitize_special(text: str) -> str:
    return _SPECIAL_RE.sub(lambda m: "<​" + (m.group(1) or m.group(2)) + ">", text or "")
