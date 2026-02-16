export default function StatCard({ label, value }) {
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900 p-4 shadow-sm">
      <p className="text-sm text-slate-400">{label}</p>
      <p className="mt-2 text-2xl font-semibold text-cyan-300">{value}</p>
    </div>
  );
}
