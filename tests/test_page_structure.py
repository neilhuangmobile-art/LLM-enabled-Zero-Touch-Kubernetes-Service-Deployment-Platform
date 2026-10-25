"""主網站的頁面結構（2026-10-23）：每個 class="page" 的頁面都要直接放在 <div class="main"> 底下。
曾經在合併時少了一個 </div>，行事曆、部署紀錄、自動修復、資源總覽被包進隱藏的專案彈窗，四頁變成空白。"""
from html.parser import HTMLParser

import pytest

web_demo = pytest.importorskip("web_demo")

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr", "path",
        "circle", "rect", "line", "polyline", "polygon", "use", "stop"}


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.pages = [], {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div" and "page" in (a.get("class") or "").split() and a.get("id"):
            self.pages[a["id"]] = self.stack[-1] if self.stack else None
        if tag not in VOID:
            self.stack.append((tag, a.get("class") or ""))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        while self.stack:
            if self.stack.pop()[0] == tag:
                break


def test_every_page_is_direct_child_of_main():
    c = web_demo.app.test_client()
    with c.session_transaction() as s:
        s["username"] = "alice"
    html = c.get("/").get_data(as_text=True)
    t = _Tree()
    t.feed(html)
    for pid in ("page-chat", "page-services", "page-trash", "page-project", "page-calendar", "page-gitops",
                "page-healer", "page-dashboard"):
        assert pid in t.pages, pid
        assert t.pages[pid] == ("div", "main"), (pid, t.pages[pid])
