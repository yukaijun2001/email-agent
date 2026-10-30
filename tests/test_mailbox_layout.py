"""Structural regressions for the independently scrolling mailbox card."""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "static"
VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class ElementParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "parent": self.stack[-1] if self.stack else None}
        self.elements.append(element)
        if tag not in VOID_ELEMENTS:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break


class MailboxLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = ElementParser()
        cls.document.feed((STATIC / "index.html").read_text(encoding="utf-8"))
        cls.css = (STATIC / "styles.css").read_text(encoding="utf-8")

    def by_id(self, element_id):
        matches = [el for el in self.document.elements if el["attrs"].get("id") == element_id]
        self.assertEqual(len(matches), 1, element_id)
        return matches[0]

    def by_class(self, class_name):
        matches = [el for el in self.document.elements if class_name in el["attrs"].get("class", "").split()]
        self.assertEqual(len(matches), 1, class_name)
        return matches[0]

    def assert_descendant(self, child, ancestor):
        parent = child["parent"]
        while parent is not None and parent is not ancestor:
            parent = parent["parent"]
        self.assertIs(parent, ancestor)

    def css_rule(self, selector):
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, selector)
        return match.group(1)

    def test_controls_list_and_footer_are_sibling_regions(self):
        panel = self.by_class("mailbox-panel")
        controls = self.by_class("mailbox-controls")
        mailbox_list = self.by_id("mailboxList")
        footer = self.by_class("mailbox-panel-foot")
        self.assertIs(controls["parent"], panel)
        self.assertIs(mailbox_list["parent"], panel)
        self.assertIs(footer["parent"], panel)
        regions = [el for el in self.document.elements if el["parent"] is panel]
        self.assertEqual([id(el) for el in regions], [id(controls), id(mailbox_list), id(footer)])
        for element_id in ("mailboxSearch", "selectVisibleMailboxes", "clearMailboxSelection", "mailboxHint"):
            self.assert_descendant(self.by_id(element_id), controls)
        for element_id in ("mailboxSelectionCount", "showAllMailboxes", "showSelectedMailboxes"):
            self.assert_descendant(self.by_id(element_id), footer)
        self.assertEqual(mailbox_list["attrs"].get("role"), "list")
        self.assertEqual(mailbox_list["attrs"].get("tabindex"), "0")

    def test_scroll_is_confined_to_the_flexible_middle_region(self):
        panel = self.css_rule(".mailbox-panel")
        mailbox_list = self.css_rule(".mailbox-list")
        self.assertRegex(panel, r"display:\s*flex\s*;")
        self.assertRegex(panel, r"flex-direction:\s*column\s*;")
        self.assertRegex(panel, r"height:\s*clamp\(")
        self.assertRegex(mailbox_list, r"flex:\s*1\s+1\s+auto\s*;")
        self.assertRegex(mailbox_list, r"min-height:\s*0\s*;")
        self.assertRegex(mailbox_list, r"overflow-y:\s*auto\s*;")
        self.assertRegex(mailbox_list, r"overscroll-behavior-y:\s*contain\s*;")
        for selector in (".mailbox-controls", ".mailbox-panel-foot"):
            self.assertRegex(self.css_rule(selector), r"flex:\s*0\s+0\s+auto\s*;")
        self.assertRegex(self.css_rule(".email-list[hidden]"), r"display:\s*none\s*;")


if __name__ == "__main__":
    unittest.main()
