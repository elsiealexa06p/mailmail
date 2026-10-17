"""Macro Reference — shows all available macros and how to use them."""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextEdit


HELP_TEXT = """
<h2>Macro Reference</h2>

<h3>Placeholders (auto-replaced per email)</h3>
<table border="1" cellpadding="6" style="border-collapse:collapse">
<tr><td><b>{email}</b></td><td>Full recipient email (e.g. john@example.com)</td></tr>
<tr><td><b>{email_user}</b></td><td>Part before @ (e.g. john)</td></tr>
<tr><td><b>{domain}</b></td><td>Part after @ (e.g. example.com)</td></tr>
</table>

<h3>Custom Macros (from Macros tab)</h3>
<p>Create a macro named e.g. <b>gruss</b> with values "Hallo", "Hi", "Guten Tag".<br>
Use in subject or body: <b>{gruss}</b> → picks random value each time.</p>

<h3>Random Strings</h3>
<table border="1" cellpadding="6" style="border-collapse:collapse">
<tr><td><b>[RANDSTR:8:a-z0-9:lower]</b></td><td>8 random chars, lowercase alphanumeric</td></tr>
<tr><td><b>[RANDSTR:4:A-Z:upper]</b></td><td>4 uppercase letters</td></tr>
<tr><td><b>[RANDSTR:6:0-9:none]</b></td><td>6 random digits</td></tr>
<tr><td><b>REF-[RANDSTR:4:a-z:upper][RANDSTR:4:0-9:none]</b></td><td>e.g. REF-XKQM7284</td></tr>
</table>
<p>Charsets: <b>a-z</b>, <b>A-Z</b>, <b>0-9</b>, <b>a-z0-9</b>, <b>A-Z0-9</b>, <b>a-zA-Z</b>, <b>a-zA-Z0-9</b></p>
<p>Cases: <b>lower</b>, <b>upper</b>, <b>none</b> (keep as-is)</p>

<h3>Sender Rotation</h3>
<p>In the Composer tab, add multiple sender names (one per line).<br>
Set "Rotate every N emails" to control how often the sender changes.<br>
Example: 3 names, rotate every 50 → each name used for 50 emails, then next.</p>

<h3>Subject Rotation</h3>
<p>Create a macro (e.g. <b>betreffe_firmaxy</b>) with multiple subjects.<br>
In Composer, set Subject to <b>{betreffe_firmaxy}</b>.<br>
Each email picks a random subject from the list.</p>

<h3>HTML Body Rotation</h3>
<p>In Composer, add multiple HTML files. Set rotation interval.<br>
Example: 3 HTML files, rotate every 5 → mails 1-5 use html1, 6-10 use html2, etc.</p>

<h3>PDF Macro (Hash Obfuscation)</h3>
<p>Enable "PDF macro" in Composer when attaching a PDF.<br>
The system fills a hidden form field with a random string per email,<br>
making each PDF have a unique file hash.</p>

<h3>Unsubscribe</h3>
<p>If a domain has an Unsub Worker deployed (see Brands tab),<br>
the system automatically adds List-Unsubscribe headers.<br>
No manual macro needed — it's handled in the MIME builder.</p>

<h3>Examples</h3>
<pre>
Subject: {betreffe_firmaxy} - [RANDSTR:6:a-z0-9:lower]
Body:    Hallo {email_user}, Ihre Ref: REF-[RANDSTR:8:A-Z0-9:upper]
</pre>
"""


class MacroHelpTab(QWidget):
    def __init__(self, db=None):
        super().__init__()
        layout = QVBoxLayout(self)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setHtml(HELP_TEXT)
        layout.addWidget(view)
