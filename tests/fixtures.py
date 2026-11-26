from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

FORM = """<!doctype html><title>Jarvis Test Form</title>
<main><h1>Engineering inquiry</h1><p>Reference code Q-107</p>
<form><label>Full name <input required name="name"></label>
<label>Notes <textarea name="notes"></textarea></label>
<label>Quotation <input type="file" name="document"></label>
<button>Submit form</button></form></main>
<script>
document.querySelector('form').addEventListener('submit', event => {
  event.preventDefault();
  const name = document.querySelector('[name=name]').value;
  document.querySelector('main').textContent = 'Form submitted for ' + name;
});
</script>"""

MAIL = """<!doctype html><title>Jarvis Local Mail Fixture</title>
<button id="compose" onclick="compose()">Compose</button>
<main><h1>Inbox</h1></main>
<script>
const params = new URLSearchParams(location.search);
let draft;
function node(tag, text, parent) {
  const el = document.createElement(tag);
  el.textContent = text;
  parent.append(el);
  return el;
}
function compose() {
  draft = node('div', '', document.body);
  draft.setAttribute('role', 'dialog');
  const input = node('input', '', draft);
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-label', 'To recipients');
  const list = node('div', '', draft);
  list.setAttribute('role', 'listbox');
  input.oninput = () => {
    list.replaceChildren();
    if (/^priya$/i.test(input.value)) {
      const one = node('div', 'Priya Rao priya.rao@example.test', list);
      one.setAttribute('role', 'option');
      const two = node('div', 'Priya Das priya.das@example.test', list);
      two.setAttribute('role', 'option');
    }
  };
  let recipient = '';
  input.onkeydown = event => {
    if (event.key === 'Enter') {
      recipient = input.value;
      const chip = node('span', recipient, draft);
      chip.dataset.jarvisRecipient = recipient;
      input.value = '';
    }
  };
  const subject = node('input', '', draft);
  subject.name = 'subjectbox';
  const body = node('div', ' ', draft);
  body.contentEditable = 'true';
  body.setAttribute('role', 'textbox');
  body.setAttribute('aria-label', 'Message Body');
  const attachment = node('input', '', draft);
  attachment.type = 'file';
  attachment.multiple = true;
  attachment.hidden = true;
  const names = node('div', '', draft);
  attachment.onchange = () => {
    names.replaceChildren();
    for (const file of attachment.files) node('span', file.name, names);
  };
  const attach = node('button', 'Attach files', draft);
  attach.setAttribute('aria-label', 'Attach files');
  attach.onclick = () => attachment.click();
  node('button', 'Send', draft).onclick = () => {
    localStorage.attempts = +(localStorage.attempts || 0) + 1;
    if (params.has('uncertain')) return;
    const sent = JSON.parse(localStorage.sent || '[]');
    sent.push({ recipient, subject: subject.value, body: body.innerText,
                attachments: [...attachment.files].map(file => file.name) });
    localStorage.sent = JSON.stringify(sent);
    draft.remove();
    node('div', 'Message sent', document.body);
  };
}
function showSent() {
  const main = document.querySelector('main');
  main.replaceChildren();
  const query = decodeURIComponent(location.hash.replace('#search/', '').replaceAll('+',' '));
  const sent = JSON.parse(localStorage.sent || '[]')
    .filter(m => query.includes('to:' + m.recipient) && query.includes('subject:"' + m.subject + '"'));
  if (!sent.length) node('p', 'No conversations found.', main);
  for (const message of sent) {
    const item = node('button', message.subject, main);
    item.onclick = () => {
      main.replaceChildren();
      node('h1', message.subject, main);
      node('div', message.body, main);
      const detail = node('button', 'Show details', main);
      detail.setAttribute('aria-label', 'Show details');
      detail.onclick = () => node('div', 'to: ' + message.recipient, main);
      for (const name of message.attachments) node('div', name, main);
    };
  }
}
if (location.hash.startsWith('#search/')) showSent();
</script>"""


@contextmanager
def fixture_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = self.path.partition("?")[0]
            data = (MAIL if route == "/mail/" else FORM).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
