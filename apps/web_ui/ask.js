/* Permission, question, and private-input prompt box (#ask). */

import { change } from "./api.js";
import { registry } from "./components.js";
import { $, el, rowOf, state } from "./dom.js";
import { showProgress } from "./timeline.js";

export function drawAsk() {
  const box = $("ask");
  const row = rowOf(state.open);
  const ask = row && row.ask;
  const waiting = Boolean(ask) || row?.state === "waiting";
  if (state.turn && (waiting || !box.hidden)) {
    showProgress(state.turn, state.turn.phase, waiting);
  }
  $("composer").hidden = !row && !state.newSession;
  $("queue").parentElement.hidden = Boolean(state.newSession) || waiting;
  $("stop").hidden = !row || row.state !== "running" || waiting;
  $("text").placeholder = waiting ? "Queue a separate message" : "Message";
  $("text").setAttribute("aria-label", $("text").placeholder);
  $("send").textContent = waiting ? "Queue message" : "Send";
  if (!ask) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  if (box.dataset.ask === ask.id && !box.hidden) return;
  box.dataset.ask = ask.id;
  box.hidden = false;
  box.replaceChildren();
  const customAsk = registry.getAsk(ask.kind);
  if (customAsk) {
    customAsk(box, ask, { change, answer, state, el });
    return;
  }
  const head = el("h2", null, ask.kind === "question" ? "Question" : "Permission");
  if (ask.about) head.title = ask.about;
  box.append(head);
  box.append(el("pre", null, ask.command || ask.tool || ""));
  if (ask.reason) box.append(el("div", null, ask.reason));
  const choices = el("div", "choices");
  for (const choice of ask.choices || []) {
    const label = (ask.choice_labels || {})[choice] || choice;
    const button = el(
      "button",
      choice === (ask.choices || [])[0] ? "primary" : "",
      ask.kind === "question" ? `${choice}. ${label}` : label,
    );
    button.type = "button";
    button.addEventListener("click", () => answer(ask, { choice }));
    choices.append(button);
  }
  box.append(choices);
  const words = el("form", "words");
  const input = el("input");
  input.placeholder = "Answer";
  input.setAttribute("aria-label", "Answer");
  const send = el("button", null, "Send");
  send.type = "submit";
  words.append(input, send);
  words.addEventListener("submit", (event) => {
    event.preventDefault();
    if (input.value.trim()) answer(ask, { text: input.value.trim() });
  });
  box.append(words);
}

export async function answer(ask, given) {
  const box = $("ask");
  for (const button of box.querySelectorAll("button")) button.disabled = true;
  try {
    await change("session/submit", {
      session: state.open,
      source: "web",
      reply_to: ask.id,
      text: given.text || given.choice,
      choice: given.choice,
    });
  } catch (error) {
    box.append(el("div", "error", error.message));
    for (const button of box.querySelectorAll("button")) button.disabled = false;
  }
}
