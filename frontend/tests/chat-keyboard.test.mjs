import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
const keyboard = app.slice(app.indexOf("document.addEventListener('keydown'"), app.indexOf("modal.addEventListener('click'"));

function press(overrides = {}) {
  let handler, submissions = 0, prevented = false;
  const context = vm.createContext({
    document: { addEventListener: (_, callback) => { handler = callback; } },
    modal: { classList: { contains: () => false } },
    state: { sending: false },
    $: () => ({ requestSubmit: () => { submissions++; } }),
  });
  vm.runInContext(keyboard, context);
  handler({ key: 'Enter', target: { id: 'question' },
    preventDefault: () => { prevented = true; }, ...overrides });
  return { submissions, prevented };
}

test('Enter submits the chat form and prevents a newline', () => {
  assert.deepEqual(press(), { submissions: 1, prevented: true });
});

test('Shift+Enter preserves a newline', () => {
  assert.deepEqual(press({ shiftKey: true }), { submissions: 0, prevented: false });
});

test('confirming an IME candidate does not send the chat', () => {
  for (const event of [{ isComposing: true }, { keyCode: 229 }]) {
    assert.deepEqual(press(event), { submissions: 0, prevented: false });
  }
});

test('keyboard sending remains scoped to the chat input', () => {
  assert.deepEqual(press({ target: { id: 'diagnostic-answer' } }), { submissions: 0, prevented: false });
});

test('Ctrl and Cmd Enter remain supported', () => {
  for (const event of [{ ctrlKey: true }, { metaKey: true }]) {
    assert.deepEqual(press(event), { submissions: 1, prevented: true });
  }
});
