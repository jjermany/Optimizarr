export default function StatCard({ label, value, subtext }) {
  return (
    <div className="group relative overflow-hidden rounded-2xl border border-slate-700/70 bg-gradient-to-br from-slate-900/95 to-slate-900/60 p-4 shadow-xl shadow-slate-950/40 transition-all duration-200 hover:-translate-y-0.5 hover:border-cyan-500/40 hover:shadow-cyan-950/30">
      <div className="pointer-events-none absolute inset-0 bg-gradient-to-br from-cyan-400/10 via-transparent to-transparent opacity-80" />
      <div className="pointer-events-none absolute -right-8 -top-8 h-20 w-20 rounded-full bg-cyan-400/10 blur-2xl transition-opacity duration-200 group-hover:opacity-100" />
      <p className="relative text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-400">{label}</p>
      <p className="relative mt-2 text-3xl font-bold leading-none text-cyan-200">{value}</p>
      {subtext && <p className="relative mt-2 text-xs text-slate-500">{subtext}</p>}
    </div>
  );
}
