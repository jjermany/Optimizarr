export default function StatCard({ label, value, subtext }) {
  return (
    <div className="group relative overflow-hidden rounded-xl border border-slate-700/60 bg-gradient-to-br from-slate-900 to-slate-800/80 p-4 shadow-lg shadow-slate-950/40 transition-all duration-200 hover:border-slate-600 hover:shadow-cyan-950/30">
      <div className="absolute inset-0 bg-gradient-to-br from-cyan-500/5 to-transparent opacity-0 transition-opacity duration-200 group-hover:opacity-100" />
      <p className="relative text-xs font-medium uppercase tracking-wider text-slate-400">{label}</p>
      <p className="relative mt-2 text-2xl font-bold text-cyan-300">{value}</p>
      {subtext && <p className="relative mt-1 text-xs text-slate-500">{subtext}</p>}
    </div>
  );
}
