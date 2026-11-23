/* Jobs component web UI: session badge, details panel, tool action, and /jobs. */

export function register(host) {
  const { look, change } = host.api;
  const { $, el, showPanel, state } = host.dom;
  const jobsMap = state.jobs || new Map();
  const stoppingJobs = state.stoppingJobs || new Set();
  const expandedJobs = state.expandedJobs || new Map();

  async function stopJob(job, button) {
    stoppingJobs.add(job.id);
    button.disabled = true;
    button.textContent = "Stopping…";
    try {
      await change("job/stop", { job: job.id });
    } catch (error) {
      stoppingJobs.delete(job.id);
      alert(error.message);
    } finally {
      button.blur();
      host.drawDetails();
    }
  }

  function jobView(job) {
    const item = el("details", "job");
    item.dataset.job = job.id;
    const status = job.stopped_by === "user" ? "Stopped by you" : job.state_text;
    item.append(el("summary", null, `${job.command} · ${status}`));
    item.append(el("div", "sub", job.id));
    if (job.exit_code !== null) {
      item.append(el("div", null, `Exit: ${job.exit_code}`));
    }
    if (job.state === "running") {
      const stop = el(
        "button",
        null,
        stoppingJobs.has(job.id) ? "Stopping…" : "Stop",
      );
      stop.type = "button";
      stop.disabled = stoppingJobs.has(job.id);
      stop.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        stopJob(job, stop);
      });
      item.querySelector("summary").append(stop);
    }
    const output = el("pre");
    item.append(output);
    item.addEventListener("toggle", async () => {
      if (!item.open) return;
      try {
        const reply = await look("job/read", { job: job.id });
        output.textContent = (reply.omitted_bytes
          ? `${reply.omitted_bytes} earlier bytes no longer retained.\n` : "")
          + (reply.output || "No output");
      } catch (error) {
        output.textContent = error.message;
      }
    });
    return item;
  }

  async function workSection(row) {
    const section = el("section");
    section.id = "background-jobs";
    section.append(el("h2", null, "Background jobs"));
    try {
      const reply = await look("job/list", { session: row.id });
      const running = reply.jobs.filter((job) => job.state === "running");
      if (running.length > 1) {
        const all = el("button", null, "Stop all");
        all.type = "button";
        all.addEventListener("click", async () => {
          all.disabled = true;
          all.textContent = "Stopping…";
          await Promise.all(running.map((job) => stopJob(job, all)));
        });
        section.append(all);
      }
      const groups = new Map();
      for (const [name, items] of [
        ["Running", running],
        ["Finished", reply.jobs.filter((job) => job.state !== "running")],
      ]) {
        const group = el("details", "job-group");
        group.dataset.group = name;
        const key = `${row.id}:${name}`;
        group.open = expandedJobs.get(key) ?? name === "Running";
        group.addEventListener("toggle", () => {
          if (group.isConnected) expandedJobs.set(key, group.open);
        });
        group.append(el("summary", null, `${name} · ${items.length}`));
        if (!items.length) group.append(el("p", "sub", "None"));
        groups.set(name, group);
        section.append(group);
      }
      for (const job of reply.jobs) {
        jobsMap.set(job.id, job);
        if (job.state !== "running") stoppingJobs.delete(job.id);
        const item = jobView(job);
        const key = `${row.id}:job:${job.id}`;
        item.open = expandedJobs.get(key) || false;
        item.addEventListener("toggle", () => {
          if (item.isConnected) expandedJobs.set(key, item.open);
        });
        groups.get(job.state === "running" ? "Running" : "Finished").append(item);
      }
    } catch (error) {
      section.append(el("div", "error", error.message));
    }
    return section;
  }

  function jobLink(id) {
    const link = el("button", null, "Show job");
    link.type = "button";
    link.addEventListener("click", () => {
      showPanel("details");
      const view = Array.from($("details").querySelectorAll("[data-job]"))
        .find((item) => item.dataset.job === id);
      if (!view) return;
      view.closest(".job-group").open = true;
      view.open = true;
      view.scrollIntoView();
    });
    return link;
  }

  host.sessionBadge((row) => (
    row.jobs ? `${row.jobs} job${row.jobs > 1 ? "s" : ""}` : null
  ));

  host.details((row) => workSection(row));

  host.toolAction((record) => {
    const fields = host.entries.fieldsOf(record);
    const jobId = fields.fields?.job_id || fields.reference;
    return jobId ? jobLink(jobId) : null;
  });

  host.command("/jobs", () => {
    if (state.newSession) {
      throw new Error("Send the first message to use this session command.");
    }
    showPanel("details");
  });
}
