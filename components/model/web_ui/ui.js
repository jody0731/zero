/* Model component web UI: Models settings tab (servers, keys, probe, connect). */

const modelKeyState = {
  there: "Saved",
  missing: "No key saved",
  environment: "From environment",
  "none needed": "—",
};

export function register(host) {
  const { look, change } = host.api;
  const { $, el, failureDetails, modelFailureView, state } = host.dom;
  const { acceptModelDirectory, nextModelVersion } = host.models;

  function modelButton(text, pressed) {
    const button = el("button", null, text);
    button.type = "button";
    button.addEventListener("click", pressed);
    return button;
  }

  function modelInput(label, type = "text", required = true) {
    const field = el("label", null, label);
    const input = el("input");
    input.type = type;
    input.required = required;
    if (type === "password") input.autocomplete = "off";
    field.append(input);
    return [field, input];
  }

  function modelForm(fields, label, submit, close, notice) {
    const form = el("form", "model-server-form");
    const save = el("button", "primary", label);
    save.type = "submit";
    const status = el("p", "error model-status");
    status.setAttribute("role", "alert");
    const buttons = el("div", "model-actions");
    buttons.append(save, modelButton("Cancel", close));
    if (notice) form.append(el("p", "sub", notice));
    form.append(...fields, buttons, status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (save.disabled) return;
      save.disabled = true;
      status.replaceChildren();
      try {
        await submit();
      } catch (error) {
        if (error.modelFailure) status.append(modelFailureView(error.modelFailure));
        else status.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    return form;
  }

  function modelKeys(server, editor, refresh, close) {
    const [field, key] = modelInput("API key", "password");
    editor.replaceChildren(modelForm([field], "Save", async () => {
      const name = server.name;
      const version = nextModelVersion(name);
      const reply = await change("model/server/key/set", { name, key: key.value });
      key.value = "";
      if (!acceptModelDirectory(name, version, reply.directory)) return;
      const saved = `Saved key for ${server.title}.`;
      const directory = reply.directory;
      const message = directory?.error
        ? `Key saved. Couldn’t load ${server.title}’s model list. `
          + "Choose View models to retry."
        : saved + (directory && !directory.models.length ? " No models listed." : "");
      await refresh(message, directory?.error);
    }, close ?? (() => editor.replaceChildren())));
    key.focus();
  }

  function removeModelKey(server, editor, refresh) {
    editor.replaceChildren(modelForm([], "Remove saved key", async () => {
      const name = server.name;
      const version = nextModelVersion(name);
      const reply = await change("model/server/key/set", { name, key: null });
      if (!acceptModelDirectory(name, version, reply.directory)) return;
      await refresh(`Removed saved key for ${server.title}.`);
    }, () => editor.replaceChildren(), `Remove the saved key for ${server.title}?`));
  }

  async function showModelNames(server, editor, status) {
    const waiting = el("p", "sub", "Requesting the model list…");
    status.textContent = "";
    editor.replaceChildren(waiting);
    const name = server.name;
    const version = nextModelVersion(name);
    try {
      const reply = await look("model/list", { server: name });
      const row = reply.servers.find((item) => item.server === name);
      if (!acceptModelDirectory(name, version, row)) return;
      if (!waiting.isConnected) return;
      if (row?.error) {
        editor.replaceChildren(el("p", "error", row.error));
        return;
      }
      const list = el("ul", "model-names");
      const models = row?.models || [];
      for (const model of models) {
        list.append(el("li", null, model.slice(server.model_prefix?.length || 0)));
      }
      editor.replaceChildren(models.length
        ? list : el("p", "sub", "No models listed."));
    } catch (error) {
      if (waiting.isConnected) {
        editor.replaceChildren(el("p", "error", error.message));
      }
    }
  }

  function modelNameInput(server) {
    const [field, model] = modelInput("Model name");
    const known = el("datalist");
    known.id = "known-models";
    const listed = state.modelDirectories.get(server.name)?.models || [];
    const ids = listed.map((item) => item.slice(server.model_prefix?.length || 0));
    for (const name of new Set([...ids, ...Object.keys(server.models || {})])) {
      const option = el("option");
      option.value = name;
      known.append(option);
    }
    model.setAttribute("list", known.id);
    field.append(known);
    return [field, model];
  }

  function modelTest(server, editor, status) {
    const [field, model] = modelNameInput(server);
    editor.replaceChildren(modelForm([field], "Run test", async () => {
      const reply = await change("model/server/test", {
        name: server.name, model: model.value.trim(),
      });
      status.textContent = `${reply.model} answered the test.`;
      editor.replaceChildren();
    }, () => editor.replaceChildren(), "The provider may charge for the test."));
    model.focus();
  }

  function connectModel(server, editor, refresh, close) {
    const [nameField, name] = modelInput("Server name");
    const [urlField, url] = modelInput("API URL", "url");
    const [modelField, model] = modelNameInput(server);
    const [contextField, tokens] = modelInput(
      "Context window in tokens",
      "number",
      false,
    );
    contextField.hidden = true;
    tokens.min = "1";
    const [keyField, key] = modelInput("API key", "password", false);
    const fields = [modelField, contextField];
    if (!server.name) fields.unshift(nameField, urlField);
    if (!server.name || server.key_state === "missing") fields.push(keyField);
    editor.replaceChildren(modelForm(fields, "Test and connect", async () => {
      const values = {
        name: server.name || name.value.trim(), model: model.value.trim(),
      };
      if (!server.name) values.url = url.value.trim();
      if (key.value) values.key = key.value;
      if (contextField.hidden) {
        let loadsWindow = false;
        try {
          const probeFields = values.url ? { url: values.url } : { name: values.name };
          if (values.key) probeFields.key = values.key;
          const directory = await look("model/server/probe", probeFields);
          if (values.url) values.url = directory.url;
          const id = values.model.startsWith(values.name + "/")
            ? values.model.slice(values.name.length + 1) : values.model;
          loadsWindow = directory.models.some(
            (entry) => entry.id === id && entry.context_reported_on_load,
          );
        } catch {
          // A directory is optional for a test connection.
        }
        if (values.url) url.value = values.url;
        const address = { name: values.name, model: values.model };
        if (values.url) address.url = values.url;
        const found = await look("model/window", address);
        if (found.context_window === null && !loadsWindow) {
          contextField.hidden = false;
          tokens.required = true;
          tokens.focus();
          return;
        }
      }
      if (tokens.value) values.context_window = Number(tokens.value);
      const reply = await change("model/server/connect", values);
      key.value = "";
      state.modelServer = values.name;
      if (reply.default) {
        const setting = state.settings.find((row) => row.name === "model");
        if (setting) setting.value = reply.model;
      }
      await refresh(`Connected ${reply.model}.`);
    }, close, "Connecting makes a short test the provider may charge for."));
    for (const field of [name, url, model]) {
      field.addEventListener("input", () => {
        contextField.hidden = true;
        tokens.required = false;
        tokens.value = "";
      });
    }
    (server.name ? model : name).focus();
  }

  async function modelServers(body, notice = "", directoryError = "") {
    const version = ++state.modelPanelVersion;
    const reply = await look("model/server/list");
    if (version !== state.modelPanelVersion || state.settingsView !== "Models"
        || !$("settings-dialog").open) return;
    const refresh = (status, error) => modelServers(body, status, error);
    draw(notice, false, directoryError);

    function draw(message, addKey = false, dirError = "") {
      const status = el("div", "sub model-status", message);
      status.setAttribute("role", "status");
      if (dirError) status.append(failureDetails([dirError]));
      body.replaceChildren(status);

      const table = el("table", "model-server-list");
      const heading = table.createTHead().insertRow();
      for (const name of ["Model server", "API key", "Actions"]) {
        const cell = el("th", null, name);
        cell.scope = "col";
        heading.append(cell);
      }
      const rows = table.createTBody();
      for (const server of reply.servers) {
        const row = rows.insertRow();
        row.dataset.modelServer = server.name;
        row.insertCell().textContent = server.title || server.name;
        row.insertCell().textContent = modelKeyState[server.key_state] || "—";
        const missing = server.key_state === "missing";
        const action = modelButton(missing ? "Add API key" : "Manage", () => {
          state.modelServer = server.name;
          draw("", missing);
        });
        action.setAttribute("aria-label", `${action.textContent} for ${server.title}`);
        row.insertCell().append(action);
      }
      body.append(table);

      const close = () => {
        state.modelServer = null;
        draw("");
      };
      const selected = reply.servers.find((row) => row.name === state.modelServer);
      if (selected) {
        const panel = el("section", "model-server-panel");
        const editor = el("div", "model-server-editor");
        panel.append(
          el("h3", null, selected.title),
          el("p", "sub model-server-url", selected.url),
        );
        if (addKey) {
          panel.append(editor);
          body.append(panel);
          modelKeys(selected, editor, refresh, close);
        } else {
          const actions = el("div", "model-actions");
          const saved = selected.has_saved_key ?? selected.key_state === "there";
          const usesKey = selected.needs_key || saved
            || selected.key_state === "environment";
          if (usesKey) {
            actions.append(modelButton(
              saved ? "Replace saved key" : "Add API key",
              () => modelKeys(selected, editor, refresh),
            ));
          }
          if (saved) {
            actions.append(modelButton(
              "Remove saved key",
              () => removeModelKey(selected, editor, refresh),
            ));
          }
          actions.append(
            modelButton("View models", () => showModelNames(selected, editor, status)),
            modelButton("Test model", () => modelTest(selected, editor, status)),
            modelButton("Connect a model", () => connectModel(
              selected, editor, refresh, () => editor.replaceChildren(),
            )),
          );
          if (selected.user_added) {
            actions.append(modelButton("Remove server", () => {
              editor.replaceChildren(modelForm([], "Remove server", async () => {
                const name = selected.name;
                const ver = nextModelVersion(name);
                await change("model/server/disconnect", { name });
                if (!acceptModelDirectory(name, ver)) return;
                state.modelServer = null;
                await refresh(`Removed ${selected.title}.`);
              }, () => editor.replaceChildren(), `Remove ${selected.title}?`));
            }));
          }
          actions.append(modelButton("Close", close));
          panel.append(actions, editor);
          body.append(panel);
          actions.querySelector("button")?.focus();
        }
      } else if (state.modelServer === "") {
        const editor = el("section", "model-server-panel");
        body.append(editor);
        connectModel({}, editor, refresh, close);
      } else {
        body.append(modelButton("Add model server", () => {
          state.modelServer = "";
          draw("");
        }));
      }
    }
  }

  host.settings("Models", (body) => modelServers(body));
}
