# Multi-tenant pilot workload contract

`pilot-v1.json` is the reviewed, synthetic workload and proposed SLO contract. It is not a
claim that the current platform meets these objectives. Run `make workload-validate` to
check the schema and display its canonical SHA-256. The root seed is 42; per-tenant,
per-class, per-trial seeds are derived from it with SHA-256 rather than relying on Python's
process-randomized hash. Generated preview records contain only lengths, delays, and
identifiers—not raw prompts, responses, recordings, or credentials.

The three tenants are independent of the four workload classes. The nominal text mix is
50% chat, 30% agentic-style, 20% batch; the voice campaign changes this to 40/30/20/10.
`agentic-style` means multi-turn synthetic tool-result replay, not genuine tool execution
or an accuracy evaluation. AIPerf drives text traffic; a separate audio harness measures
the ASR-to-LLM-to-TTS path and interruption. All multi-turn input context is capped at
6,144 tokens; truncation is counted and published.

Measurement boundaries are fixed:

- **Offered**: scheduled client arrival. **Sent**: HTTP request starts. Their delta is
  client-side scheduling delay.
- **Admitted**: the trusted gateway authorizes the request. **Queued**: llm-d holds it in
  flow control. **Dispatched**: llm-d selects a ready model endpoint.
- **Completed**: a text stream ends with `[DONE]`, or a voice turn emits its final audio
  segment. **Rejected**: admission denies the request. **Failed**: transport, runtime,
  timeout, or stream failure after admission. End-of-window in-flight work is separate.
- Text TTFT is from client send to the first non-empty content delta; TPOT is the mean
  spacing of subsequent generated tokens for that request. Voice first-audio latency is
  from detected end-of-utterance to the first decodable audio segment. Interruption
  latency ends when no further audio is emitted.

P95 is publishable only with at least 100 completed samples for that class and trial;
P99 is diagnostic unless at least 1,000 samples are present. Every report must disclose
offered, sent, admitted, queued, dispatched, completed, rejected, failed, and still-in-flight
counts, together with reconciliation warnings. Warmup and cooldown are excluded from
performance percentiles but included in rental-cost totals.
