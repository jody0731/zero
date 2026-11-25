/* Media component web UI: live composer Mic dictation and Listen playback. */

function encodeBase64(bytes) {
  let binary = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(binary);
}

async function fetchJson(url, opts = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...opts,
  });
  let body = {};
  try {
    body = await response.json();
  } catch {
    // Non-JSON error body.
  }
  if (!response.ok || body.ok === false) {
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return body;
}

export function register(host) {
  const { el, state } = host.dom;
  const modelUtilities = host.api?.modelUtilities;

  let activeDictation = null;
  let micButton = null;
  let composerTextarea = null;
  let composerCtx = null;

  async function refreshMicAvailability() {
    if (!micButton || activeDictation) return;
    if (!modelUtilities) return;
    const draft = state.newSession;
    const runs = state.runsWith || {};
    const model = draft?.model || runs.model || state.hello?.model || null;
    const sttSetting = draft?.stt_service || runs.stt_service || "current_server";
    if (sttSetting === "off") {
      micButton.disabled = true;
      micButton.title = "Speech to text is off";
      return;
    }
    const reply = await modelUtilities(model);
    if (!reply || activeDictation) return;
    const cap = reply.capabilities?.stt_service;
    if (!cap) return;
    const usable = sttSetting === "current_server"
      ? Boolean(cap.current_server_service)
      : (cap.services || []).some((svc) => svc.name === sttSetting && svc.available);
    micButton.disabled = !usable;
    micButton.title = usable ? "Dictate" : "Speech to text unavailable";
  }

  function formatDictationText(prefix, transcript) {
    const clean = (transcript || "").trim();
    const joiner = prefix && clean && !/\s$/.test(prefix) ? " " : "";
    return prefix + joiner + clean;
  }

  function isOriginActive(dict) {
    return (
      (state.openVersion || 0) === dict.originOpenVersion
      && state.open === dict.originOpen
      && state.newSession === dict.originNewSession
    );
  }

  function applyDictationText(dict, transcript) {
    const clean = (transcript || "").trim();
    if (!clean) return;
    const combined = formatDictationText(dict.prefix, clean);
    if (dict.originOpen && !dict.originNewSession) {
      if (state.open === dict.originOpen && !state.newSession) {
        if (!composerTextarea) return;
        composerTextarea.value = combined;
        composerTextarea.dispatchEvent(new Event("input", { bubbles: true }));
        return;
      }
      if (composerCtx?.writeDraft) {
        composerCtx.writeDraft(dict.originOpen, combined);
      }
      return;
    }
    if (isOriginActive(dict)) {
      if (!composerTextarea) return;
      composerTextarea.value = combined;
      composerTextarea.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  async function cleanupDictation(dict) {
    if (!dict) return;
    if (dict.events) {
      dict.events.close();
      dict.events = null;
    }
    if (dict.worklet) {
      try { dict.worklet.disconnect(); } catch { /* ignore */ }
      dict.worklet = null;
    }
    if (dict.source) {
      try { dict.source.disconnect(); } catch { /* ignore */ }
      dict.source = null;
    }
    if (dict.stream) {
      for (const track of dict.stream.getTracks()) track.stop();
      dict.stream = null;
    }
    if (dict.audioCtx) {
      try { await dict.audioCtx.close(); } catch { /* ignore */ }
      dict.audioCtx = null;
    }
  }

  async function stopDictation(cancel = false) {
    const dict = activeDictation;
    if (!dict) return;
    activeDictation = null;
    if (micButton) {
      micButton.textContent = "Mic";
      micButton.classList.remove("recording");
      micButton.setAttribute("aria-pressed", "false");
    }
    await cleanupDictation(dict);
    if (dict.inFlight) {
      await dict.inFlight.catch(() => {});
    }
    const id = encodeURIComponent(dict.id);
    try {
      if (cancel) {
        await fetchJson(`/api/dictation/${id}/cancel`, { method: "POST" });
      } else {
        const reply = await fetchJson(`/api/dictation/${id}/stop`, {
          method: "POST",
        });
        if (typeof reply.text === "string" && reply.text.trim()) {
          applyDictationText(dict, reply.text);
        }
      }
    } catch (error) {
      if (!cancel) alert(error.message);
    } finally {
      refreshMicAvailability();
    }
  }

  async function startDictation() {
    if (!composerTextarea || !micButton) return;
    if (activeDictation) {
      await stopDictation(false);
      return;
    }
    const draft = state.newSession;
    const runs = state.runsWith || {};
    const originOpen = state.open;
    const originNewSession = draft;
    const originOpenVersion = state.openVersion || 0;
    const originPrefix = composerTextarea.value;
    const payload = {};
    if (originOpen && !draft) {
      payload.session = originOpen;
    } else {
      if (draft?.model || runs.model) payload.model = draft?.model || runs.model;
      if (draft?.stt_service || runs.stt_service) {
        payload.stt_service = draft?.stt_service || runs.stt_service;
      }
    }

    micButton.disabled = true;
    try {
      const started = await fetchJson("/api/dictation", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const dict = {
        id: started.dictation_id,
        prefix: originPrefix,
        originOpen,
        originNewSession,
        originOpenVersion,
        seq: 0,
        inFlight: Promise.resolve(),
        events: null,
        stream: null,
        audioCtx: null,
        source: null,
        worklet: null,
      };
      if (!isOriginActive(dict)) {
        await fetchJson(
          `/api/dictation/${encodeURIComponent(dict.id)}/cancel`,
          { method: "POST" },
        ).catch(() => {});
        micButton.disabled = false;
        refreshMicAvailability();
        return;
      }
      activeDictation = dict;
      micButton.disabled = false;
      micButton.textContent = "Stop";
      micButton.classList.add("recording");
      micButton.setAttribute("aria-pressed", "true");

      const events = new EventSource(
        `/api/dictation/${encodeURIComponent(dict.id)}/events`,
      );
      dict.events = events;
      const onDelta = (event) => {
        if (activeDictation !== dict) return;
        try {
          const data = JSON.parse(event.data);
          const shown = data.full_text ?? data.text ?? "";
          if (shown) applyDictationText(dict, shown);
        } catch {
          // Ignore malformed event payloads.
        }
      };
      events.addEventListener("interim", onDelta);
      events.addEventListener("final", onDelta);
      events.addEventListener("error", (event) => {
        if (activeDictation !== dict || !event.data) return;
        try {
          const data = JSON.parse(event.data);
          if (data.error) alert(data.error);
        } catch {
          // Ignore transport reconnect errors.
        }
      });

      if (navigator.mediaDevices?.getUserMedia && typeof AudioContext !== "undefined") {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (activeDictation !== dict) {
          for (const track of stream.getTracks()) track.stop();
          return;
        }
        dict.stream = stream;
        const audioCtx = new AudioContext();
        dict.audioCtx = audioCtx;
        await audioCtx.audioWorklet.addModule("/ui/components/media/mic_worklet.js");
        if (activeDictation !== dict) {
          await cleanupDictation(dict);
          return;
        }
        const source = audioCtx.createMediaStreamSource(stream);
        const worklet = new AudioWorkletNode(audioCtx, "zero-mic-pcm");
        dict.source = source;
        dict.worklet = worklet;
        worklet.port.onmessage = (msg) => {
          if (activeDictation !== dict || !msg.data?.pcm) return;
          const bytes = new Uint8Array(msg.data.pcm);
          const seq = dict.seq;
          dict.seq += 1;
          const rate = Math.round(msg.data.sampleRate || audioCtx.sampleRate || 16000);
          const encoded = encodeBase64(bytes);
          dict.inFlight = dict.inFlight.then(() => fetchJson(
            `/api/dictation/${encodeURIComponent(dict.id)}/chunk`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ seq, data: encoded, sample_rate: rate }),
            },
          )).catch(() => {});
        };
        source.connect(worklet);
        // Connect silently to destination so audio graph ticks across browsers.
        const gain = audioCtx.createGain();
        gain.gain.value = 0;
        worklet.connect(gain);
        gain.connect(audioCtx.destination);
      }
    } catch (error) {
      if (activeDictation) {
        await stopDictation(true);
      } else {
        micButton.disabled = false;
        micButton.textContent = "Mic";
      }
      alert(error.message);
      refreshMicAvailability();
    }
  }

  host.composer((ctx) => {
    const { textarea, controls } = ctx;
    if (!controls || !textarea) return;
    composerCtx = ctx;
    composerTextarea = textarea;
    const button = el("button", "ghost mic-button", "Mic");
    button.type = "button";
    button.setAttribute("aria-label", "Dictate");
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      startDictation();
    });
    micButton = button;
    controls.append(button);
    refreshMicAvailability();
    window.addEventListener("zero:session-changed", () => refreshMicAvailability());
  });

  host.command("/dictate", async () => {
    await startDictation();
    return true;
  });
}
