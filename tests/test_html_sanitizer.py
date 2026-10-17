import unittest

from scripts.html_sanitizer import document, neutralize, text_to_html, url_allowed


def body(html, **kwargs):
    return neutralize(html, **kwargs).body


class NeutralizeTests(unittest.TestCase):
    def assertNoScript(self, output):
        lowered = output.lower()
        for needle in ("<script", "javascript:", "vbscript:", "onerror", "onload", "onclick",
                       "onmouseover", "srcdoc", "http-equiv", "<meta", "<embed"):
            self.assertNotIn(needle, lowered, output)

    def test_everything_visual_is_kept(self):
        html = ('<style>.hero{background:url(https://cdn.example/bg.png)} @import url(https://cdn.example/x.css);</style>'
                '<div class="hero" style="color:#c00;font-family:Brand">'
                '<img src="https://cdn.example/logo.png" srcset="https://cdn.example/logo@2x.png 2x" width="120">'
                '<table background="https://cdn.example/t.png"><tr><td>格</td></tr></table>'
                '<video src="https://cdn.example/v.mp4" poster="https://cdn.example/p.jpg" controls></video>'
                '<iframe src="https://www.youtube.com/embed/x"></iframe>'
                '<form><input type="text" value="q"><button>Go</button></form></div>')
        output = body(html)
        for kept in ("@import url(https://cdn.example/x.css)", "background:url(https://cdn.example/bg.png)",
                     'src="https://cdn.example/logo.png"', 'srcset="https://cdn.example/logo@2x.png 2x"',
                     'background="https://cdn.example/t.png"', "<video", 'poster="https://cdn.example/p.jpg"',
                     '<iframe src="https://www.youtube.com/embed/x">', '<input type="text" value="q">',
                     "<button>Go</button>", 'style="color:#c00;font-family:Brand"'):
            self.assertIn(kept, output)

    def test_remote_stylesheets_are_kept(self):
        output = body('<head><link rel="stylesheet" href="https://cdn.example/a.css">'
                      '<link rel="preload" href="https://cdn.example/b.js"></head>')
        self.assertEqual(output, '<link rel="stylesheet" href="https://cdn.example/a.css">')

    def test_scripts_and_handlers_are_removed(self):
        output = body('<div onclick="x()">a<script>alert(1)</script><img src=x onerror=alert(1)></div>'
                      '<svg onload="alert(2)"><a href="javascript:alert(3)"><text>t</text></a>'
                      '<animate attributeName="href" to="javascript:alert(4)"/></svg>'
                      '<iframe srcdoc="<script>x</script>"></iframe>'
                      '<meta http-equiv="refresh" content="0;url=https://evil.example"><embed src="x.swf">')
        self.assertNoScript(output)
        self.assertIn("<div>a<img src=\"x\"></div>", output)

    def test_unsafe_urls_are_removed(self):
        for href in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "java\tscript:alert(1)",
                     " javascript:alert(1)", "vbscript:x", "data:text/html,<script>x</script>"):
            self.assertEqual(body(f'<a href="{href}">x</a>'), '<a target="_blank" rel="noopener noreferrer">x</a>', href)
        self.assertEqual(body('<a href="java&#9;script:alert(1)">x</a>'), '<a target="_blank" rel="noopener noreferrer">x</a>')
        self.assertEqual(body('<iframe src="data:text/html,x"></iframe>'), "<iframe></iframe>")
        self.assertEqual(body('<form action="javascript:x"></form>'), "<form></form>")

    def test_links_open_in_new_tab(self):
        output = body('<a href="https://example.com/?a=1&b=2" target="_self">ok</a>')
        self.assertEqual(output, '<a href="https://example.com/?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">ok</a>')
        self.assertTrue(url_allowed("mailto:a@b.c", media=False))

    def test_data_media_allowed_but_not_documents(self):
        self.assertIn('src="data:image/svg+xml;base64,PHN2Zz4="', body('<img src="data:image/svg+xml;base64,PHN2Zz4=">'))
        self.assertEqual(body('<img src="data:text/html;base64,PHNjcmlwdD4=">'), "<img>")

    def test_attribute_values_are_escaped(self):
        output = body('<p title="&quot;><script>alert(1)</script>">x</p>')
        self.assertNoScript(output)
        self.assertIn("&quot;&gt;&lt;script&gt;", output)

    def test_style_block_cannot_be_closed_early(self):
        output = body("<style>p{color:red}</style><p>t</p>")
        self.assertEqual(output, "<style>p{color:red}</style><p>t</p>")
        self.assertNotIn("</script", body('<style>a{content:"</script><script>x</script>"}</style>').lower())

    def test_cid_images_resolve_everywhere(self):
        images = {"logo@mail": "data:image/png;base64,AAAA"}
        rendered = neutralize('<img src="cid:LOGO@mail"><td style="background:url(cid:logo@mail)">'
                              '<img src="cid:missing">', images)
        self.assertIn('<img src="data:image/png;base64,AAAA">', rendered.body)
        self.assertIn('background:url(&quot;data:image/png;base64,AAAA&quot;)', rendered.body)
        self.assertEqual(rendered.used_cids, {"logo@mail"})

    def test_body_attributes_and_base_are_kept(self):
        rendered = neutralize('<html><head><base href="https://news.example/"></head>'
                              '<body bgcolor="#eee" style="margin:0" onload="x()"><p>t</p></body></html>')
        self.assertEqual(rendered.body_attrs, ['bgcolor="#eee"', 'style="margin:0"'])
        self.assertEqual(rendered.base_href, "https://news.example/")
        output = document(rendered.body, rendered.body_attrs, rendered.base_href)
        self.assertIn('<base href="https://news.example/" target="_blank">', output)
        self.assertIn('<body bgcolor="#eee" style="margin:0"><p>t</p></body>', output)

    def test_noscript_content_is_shown(self):
        self.assertEqual(body("<noscript><p>shown</p></noscript>"), "<p>shown</p>")

    def test_text_parts_become_rich_text(self):
        output = text_to_html("Hi <b>\nSee https://example.com/a?b=1.")
        self.assertIn("Hi &lt;b&gt;", output)
        self.assertIn('<a href="https://example.com/a?b=1" target="_blank"', output)
        self.assertTrue(output.endswith(".</div>"))


if __name__ == "__main__":
    unittest.main()
