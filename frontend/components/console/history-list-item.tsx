import { cn } from "@/lib/utils";

type HistoryListItemProps = {
  active?: boolean;
  title: string;
  onClick: () => void;
};

export function HistoryListItem({ active, title, onClick }: HistoryListItemProps) {
  return (
    <button
      className={cn(
        "w-full rounded-lg border px-3 py-2 text-left transition-colors duration-150",
        active
          ? "border-foreground bg-background shadow-[inset_3px_0_0_var(--surface-accent-strong)]"
          : "border-transparent bg-transparent hover:border-border hover:bg-muted/50",
      )}
      onClick={onClick}
      type="button"
    >
      <strong className="block truncate text-[13px] font-medium text-foreground">{title}</strong>
    </button>
  );
}
