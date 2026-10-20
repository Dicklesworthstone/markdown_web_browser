// Shared browser access and rendering boundaries. DOMPurify 3.4.16 is vendored
// in purify.min.js under the adjacent license; do not insert untrusted HTML
// before sanitizing it. Safe HTML tables, SVG and MathML retain their markup.
export function safeHtml(value) {
  if (!window.DOMPurify?.isSupported) {
    throw new Error('The HTML sanitizer could not load. Reload this page.');
  }
  const fragment = window.DOMPurify.sanitize(String(value), {
    RETURN_DOM_FRAGMENT: true,
    SANITIZE_NAMED_PROPS: true,
    FORBID_TAGS: ['style', 'form', 'button', 'textarea', 'select'],
    FORBID_ATTR: ['style', 'srcset'],
  });
  for (const element of fragment.querySelectorAll('*')) {
    if (element.tagName === 'INPUT') {
      if (element.type === 'checkbox') element.disabled = true;
      else element.remove();
    }
    for (const name of ['href', 'src', 'xlink:href', 'action', 'formaction', 'poster']) {
      if (!element.hasAttribute(name)) continue;
      try {
        const url = new URL(element.getAttribute(name), window.location.href);
        const allowed = name === 'href' ? ['http:', 'https:', 'mailto:', 'tel:'] : ['http:', 'https:'];
        if (!allowed.includes(url.protocol)) element.removeAttribute(name);
      } catch {
        element.removeAttribute(name);
      }
    }
    if (element.tagName === 'A') element.setAttribute('rel', 'noopener noreferrer');
  }
  return fragment;
}

export function safeLink(value) {
  try {
    const url = new URL(value, window.location.href);
    return ['http:', 'https:', 'mailto:', 'tel:'].includes(url.protocol) ? url.href : '#';
  } catch {
    return '#';
  }
}

export function renderSafeHtml(element, value) {
  try {
    element.replaceChildren(safeHtml(value));
  } catch (error) {
    // A missing dependency must never turn into an unsafe fallback.
    element.textContent = String(value);
    console.error(error);
  }
}

export async function setupAuthentication(onChange = () => {}) {
  const response = await fetch('/auth/config');
  if (!response.ok) throw new Error('Could not load authentication settings');
  const config = await response.json();
  if (!config.required) return;
  const form = document.createElement('form');
  form.setAttribute('aria-label', 'API access');
  const label = document.createElement('label');
  label.textContent = 'API key ';
  const input = document.createElement('input');
  input.type = 'password';
  input.autocomplete = 'off';
  input.required = true;
  input.setAttribute('aria-label', 'API key');
  const button = document.createElement('button');
  button.type = 'submit';
  button.textContent = 'Sign in';
  const logout = document.createElement('button');
  logout.type = 'button';
  logout.textContent = 'Sign out';
  const message = document.createElement('span');
  message.setAttribute('role', 'status');
  label.append(input);
  form.append(label, button, logout, message);
  document.body.prepend(form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const key = input.value;
    input.value = '';
    const result = await fetch('/auth/session', {
      method: 'POST', headers: { [config.header]: key },
    });
    message.textContent = result.ok ? ' Signed in' : ' Sign-in failed';
    if (result.ok) onChange();
  });
  logout.addEventListener('click', async () => {
    const result = await fetch('/auth/session', { method: 'DELETE' });
    message.textContent = result.ok ? ' Signed out' : ' Sign-out failed';
    if (result.ok) onChange();
  });
}
