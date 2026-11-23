/* Vault component web UI: masked secret ask renderer. */

export function register(host) {
  host.ask("secret", (box, ask, ctx = {}) => {
    const el = ctx.el || host.dom.el;
    const change = ctx.change || host.api.change;
    const state = ctx.state || host.dom.state;

    box.append(el("h2", null, ask.about || "Private information"));
    if (ask.about && ask.why) {
      box.append(el("p", "private-instructions", ask.why));
    }
    box.append(el(
      "p",
      null,
      "Saved encrypted on this device until you delete it. "
        + "The value stays outside the conversation.",
    ));
    const form = el("form", "words vault-fields");
    const fields = new Map();
    for (const part of ask.choices || []) {
      const words = (ask.choice_labels || {})[part] || part;
      const label = el("label", "private-instructions", words);
      const input = el("input");
      input.type = "password";
      input.autocomplete = "new-password";
      input.required = true;
      input.setAttribute("aria-label", words.split("\n")[0]);
      label.append(input);
      form.append(label);
      fields.set(part, input);
    }
    const save = el("button", "primary", "Continue");
    save.type = "submit";
    const cancel = el("button", null, "Cancel");
    cancel.type = "button";

    async function submit(cancelled) {
      const values = {};
      for (const [part, input] of fields) {
        values[part] = input.value;
        input.value = "";
      }
      save.disabled = cancel.disabled = true;
      try {
        await change("vault/answer", {
          session: state.open,
          ask: ask.id,
          ...(cancelled ? { cancel: true } : { values }),
        });
      } catch (error) {
        box.append(el("div", "error", error.message));
        save.disabled = cancel.disabled = false;
      } finally {
        for (const part of Object.keys(values)) delete values[part];
      }
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      submit(false);
    });
    cancel.addEventListener("click", () => submit(true));
    form.append(save, cancel);
    box.append(form);
  });
}
