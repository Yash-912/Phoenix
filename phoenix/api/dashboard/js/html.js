// Tagged-template HTML with escaping by default. Views are pure functions from data to
// markup, which is what lets them be tested in node without a DOM.

class Safe {
  constructor(text) {
    this.text = text;
  }
  toString() {
    return this.text;
  }
}

const ENTITIES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function esc(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ENTITIES[ch]);
}

// Mark a string as already-safe markup. Only for markup this code wrote itself.
export function raw(text) {
  return new Safe(String(text));
}

function render(value) {
  if (value === null || value === undefined || value === false) return "";
  if (value instanceof Safe) return value.text;
  if (Array.isArray(value)) return value.map(render).join("");
  return esc(value);
}

export function html(strings, ...values) {
  let out = "";
  strings.forEach((chunk, i) => {
    out += chunk;
    if (i < values.length) out += render(values[i]);
  });
  return new Safe(out);
}

export function toString(markup) {
  return render(markup);
}
