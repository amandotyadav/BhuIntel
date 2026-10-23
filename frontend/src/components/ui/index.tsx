import { createContext, forwardRef, useContext, useId, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode,
  type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { errorMessage } from "../../api/client";

const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

type Variant = "primary" | "secondary" | "danger" | "ghost" | "success";
const VARIANTS: Record<Variant, string> = {
  primary: "bg-brand-600 text-white hover:bg-brand-700 focus-visible:ring-brand-500",
  secondary: "bg-white text-slate-800 border border-slate-300 hover:bg-slate-50 focus-visible:ring-slate-400",
  danger: "bg-rose-600 text-white hover:bg-rose-700 focus-visible:ring-rose-500",
  success: "bg-emerald-600 text-white hover:bg-emerald-700 focus-visible:ring-emerald-500",
  ghost: "text-slate-700 hover:bg-slate-100 focus-visible:ring-slate-400",
};

export function Button({ variant = "primary", loading, className, children, disabled, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; loading?: boolean }) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium shadow-sm",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50",
        VARIANTS[variant], className)}
    >
      {loading && <Spinner small />}
      {children}
    </button>
  );
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode;
  children: ReactNode; className?: string }) {
  return (
    <section className={cx("rounded-lg border border-slate-200 bg-white shadow-sm", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-slate-100 px-4 py-2.5">
          {typeof title === "string" ? <h2 className="text-sm font-semibold text-slate-800">{title}</h2> : title}
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

const TONES = {
  gray: "bg-slate-100 text-slate-700 ring-slate-200",
  green: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  red: "bg-rose-50 text-rose-800 ring-rose-200",
  amber: "bg-amber-50 text-amber-800 ring-amber-200",
  blue: "bg-sky-50 text-sky-800 ring-sky-200",
  violet: "bg-violet-50 text-violet-800 ring-violet-200",
};
export type Tone = keyof typeof TONES;

export function Badge({ tone = "gray", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={cx("inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset", TONES[tone])}>
      {children}
    </span>
  );
}

const STATUS_TONE: Record<string, Tone> = {
  succeeded: "green", processed: "green", approved: "green", accepted: "green", applied: "green", completed: "green",
  active: "green", trained: "green", match: "green",
  failed: "red", rejected: "red", unavailable: "red", critical: "red", high: "red",
  running: "blue", processing: "blue", queued: "blue", inspected: "blue",
  needs_crs: "amber", pending: "amber", deferred: "amber", possible: "amber", partial: "amber", medium: "amber",
  proposed: "amber", insufficient_data: "amber", unauthorized: "amber", manual: "amber", no_coverage: "amber",
  synthetic: "violet", derived: "violet", low: "gray", non_match: "gray",
};

export function StatusBadge({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  return <Badge tone={STATUS_TONE[value] ?? "gray"}>{value.replace(/_/g, " ")}</Badge>;
}

export function Spinner({ small }: { small?: boolean }) {
  return (
    <span role="status" aria-label="Loading"
      className={cx("inline-block animate-spin rounded-full border-2 border-current border-r-transparent",
        small ? "h-3.5 w-3.5" : "h-5 w-5 text-brand-600")} />
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <div className="flex items-center gap-2 p-4 text-sm text-slate-500"><Spinner /> {label}</div>;
}

export function ErrorBanner({ error, title = "Something went wrong" }: { error: unknown; title?: string }) {
  if (!error) return null;
  return (
    <div role="alert" className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
      <strong className="font-semibold">{title}: </strong>{errorMessage(error)}
    </div>
  );
}

export function Notice({ tone = "blue", children }: { tone?: "blue" | "amber" | "green"; children: ReactNode }) {
  const t = { blue: "border-sky-200 bg-sky-50 text-sky-900", amber: "border-amber-200 bg-amber-50 text-amber-900",
    green: "border-emerald-200 bg-emerald-50 text-emerald-900" }[tone];
  return <div role="note" className={cx("rounded-md border px-3 py-2 text-sm", t)}>{children}</div>;
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-slate-300 p-6 text-center">
      <p className="text-sm font-medium text-slate-700">{title}</p>
      {children && <div className="mt-1 text-sm text-slate-500">{children}</div>}
    </div>
  );
}

// Field provides an id that the first Input/Select/Textarea inside it adopts, so every label is associated.
const FieldIdContext = createContext<{ id: string } | null>(null);

function useFieldId(explicit?: string): string | undefined {
  const ctx = useContext(FieldIdContext);
  if (explicit || !ctx) return explicit;
  return ctx.id;
}

export function Field({ label, hint, error, children, htmlFor }: { label: string; hint?: string; error?: string;
  children: ReactNode; htmlFor?: string }) {
  const auto = useId();
  const id = htmlFor ?? auto;
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-xs font-medium text-slate-700">{label}</label>
      <FieldIdContext.Provider value={{ id }}>{children}</FieldIdContext.Provider>
      {hint && !error && <p className="text-xs text-slate-500">{hint}</p>}
      {error && <p className="text-xs text-rose-700" role="alert">{error}</p>}
    </div>
  );
}

const inputCls = "block w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm shadow-sm " +
  "focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:bg-slate-100";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input(p, ref) {
  const id = useFieldId(p.id);
  // a control with its own aria-label (e.g. a secondary file picker) keeps it instead of the field label
  return <input ref={ref} {...p} id={p["aria-label"] && !p.id ? undefined : id} className={cx(inputCls, p.className)} />;
});

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select(p, ref) {
  const id = useFieldId(p.id);
  return <select ref={ref} {...p} id={id} className={cx(inputCls, p.className)} />;
});

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea(p, ref) {
    const id = useFieldId(p.id);
    return <textarea ref={ref} {...p} id={id} className={cx(inputCls, "font-mono text-xs", p.className)} />;
  });

// forwardRef is required so react-hook-form's register() ref reaches the <input>.
export const LabeledInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { label: string;
  hint?: string; error?: string }>(function LabeledInput({ label, hint, error, ...rest }, ref) {
  const id = useId();
  return (
    <Field label={label} hint={hint} error={error} htmlFor={id}>
      <Input ref={ref} id={id} aria-invalid={error ? true : undefined} {...rest} />
    </Field>
  );
});

export function Progress({ value, label }: { value: number; label?: string }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className="w-full" aria-label={label ?? "progress"} role="progressbar" aria-valuenow={pct} aria-valuemin={0}
      aria-valuemax={100}>
      <div className="h-1.5 w-full overflow-hidden rounded bg-slate-200">
        <div className="h-full bg-brand-500 transition-all" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function Table({ head, children, empty }: { head: ReactNode[]; children: ReactNode; empty?: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead className="bg-slate-50">
          <tr>{head.map((h, i) => <th key={i} scope="col" className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-500">{h}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
      {empty && <p className="p-4 text-center text-sm text-slate-500">No rows.</p>}
    </div>
  );
}

export function Td({ children, className }: { children?: ReactNode; className?: string }) {
  return <td className={cx("px-3 py-2 align-top text-slate-700", className)}>{children}</td>;
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold text-slate-900">{value}</p>
      {hint && <p className="text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

export function JsonView({ value }: { value: unknown }) {
  return <pre className="max-h-64 overflow-auto rounded bg-slate-900 p-2 text-xs text-slate-100">{JSON.stringify(value, null, 2)}</pre>;
}

export function fmtNum(v: unknown, digits = 2): string {
  return typeof v === "number" && Number.isFinite(v) ? v.toFixed(digits) : "—";
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

export function fmtDate(s: string | null | undefined): string {
  return s ? new Date(s).toLocaleString() : "—";
}
