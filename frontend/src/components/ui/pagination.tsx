import { ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";

export interface PaginationProps {
  /** 当前页（从 1 起），超出范围时由调用方收敛 */
  page: number;
  /** 总条目数 */
  total: number;
  pageSize: number;
  onPageChange: (page: number) => void;
}

/** 分页条（规范 4.4 表格子布局）：左侧条目区间，右侧上一页 / 页码 / 下一页；条目未超一页时不渲染 */
export function Pagination({ page, total, pageSize, onPageChange }: PaginationProps) {
  const { t } = useTranslation();
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return null;
  const start = (page - 1) * pageSize + 1;
  const end = Math.min(total, page * pageSize);
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-surface-3 px-6 py-3 dark:border-ink-900">
      <p className="text-xs text-ink-500 dark:text-surface-4">{t("common.pageRange", { start, end, total })}</p>
      <div className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          icon={<ChevronLeft className="h-4 w-4" aria-hidden="true" />}
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          {t("common.prevPage")}
        </Button>
        <span className="px-1 text-xs tabular-nums text-ink-500 dark:text-surface-4">
          {t("common.pageOf", { page, pageCount })}
        </span>
        <Button variant="ghost" size="sm" disabled={page >= pageCount} onClick={() => onPageChange(page + 1)}>
          {t("common.nextPage")}
          <ChevronRight className="h-4 w-4" aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}
