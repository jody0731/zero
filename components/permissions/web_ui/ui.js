/* Permissions component web UI: /permissions command and grants dialog. */

export function register(host) {
  const { look, change } = host.api;
  const { actionDialog, el, state } = host.dom;

  async function permissionsView(session) {
    const reply = await look("grant/list", { session });
    const dialog = actionDialog("Permissions");
    for (const grant of reply.grants) {
      const section = el("section");
      section.append(el("p", null, grant.description));
      const forget = el("button", null, "Forget");
      forget.addEventListener("click", async () => {
        try {
          await change("grant/forget", { session, grant: grant.id });
          section.remove();
        } catch (error) {
          alert(error.message);
        }
      });
      section.append(forget);
      dialog.append(section);
    }
    if (!reply.grants.length) dialog.append(el("p", null, "No saved grants"));
  }

  host.command("/permissions", async ({ value, session }) => {
    if (state.newSession) {
      throw new Error("Send the first message to use this session command.");
    }
    if (value.startsWith("forget ")) {
      const reply = await look("grant/list", { session });
      const index = Number(value.slice(7));
      const grant = Number.isInteger(index) && reply.grants[index - 1];
      if (!grant) throw new Error("Choose a listed permission number");
      await change("grant/forget", { session, grant: grant.id });
    } else {
      await permissionsView(session);
    }
  });
}
