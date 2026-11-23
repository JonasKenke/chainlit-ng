import { IDataframeElement } from '@chainlit-ng/react-client';
import {
  createColumnHelper,
  createPaginatedRowModel,
  createSortedRowModel,
  flexRender,
  rowPaginationFeature,
  rowSortingFeature,
  tableFeatures,
  useTable
} from '@tanstack/react-table';
import { ArrowDown, ArrowUp } from 'lucide-react';
import { useCallback, useMemo } from 'react';

import Alert from '@/components/Alert';
import { Loader } from '@/components/Loader';
import { useTranslation } from '@/components/i18n/Translator';
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious
} from '@/components/ui/pagination';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from '@/components/ui/table';

import { useFetch } from 'hooks/useFetch';

interface DataframeData {
  index: (string | number)[];
  columns: string[];
  data: (string | number)[][];
}

type DataframeRow = Record<string, string | number>;

const features = tableFeatures({
  rowSortingFeature,
  sortedRowModel: createSortedRowModel(),
  rowPaginationFeature,
  paginatedRowModel: createPaginatedRowModel()
});
const columnHelper = createColumnHelper<typeof features, DataframeRow>();

const _DataframeElement = ({ data }: { data: DataframeData }) => {
  const { t } = useTranslation();
  const { index, columns, data: rowData } = data;

  const tableColumns = useMemo(
    () =>
      columnHelper.columns(
        columns.map((col) =>
          columnHelper.accessor(col, {
            header: ({ column }) => {
              const sort = column.getIsSorted();
              return (
                <div
                  className="flex items-center cursor-pointer"
                  onClick={() => column.toggleSorting()}
                >
                  {col}
                  {sort === 'asc' && <ArrowUp className="ml-2 !size-3" />}
                  {sort === 'desc' && <ArrowDown className="ml-2 !size-3" />}
                </div>
              );
            }
          })
        )
      ),
    [columns]
  );

  const tableRows = useMemo<DataframeRow[]>(
    () =>
      rowData.map((row, idx) => {
        const rowObj: DataframeRow = { id: index[idx] };
        columns.forEach((col, colIdx) => {
          rowObj[col] = row[colIdx];
        });
        return rowObj;
      }),
    [rowData, columns, index]
  );

  const table = useTable({
    features,
    data: tableRows,
    columns: tableColumns,
    initialState: {
      pagination: { pageIndex: 0, pageSize: 10 }
    }
  });

  const renderPaginationItems = useCallback(() => {
    return Array.from({ length: table.getPageCount() }, (_, i) => (
      <PaginationItem key={i}>
        <PaginationLink
          onClick={() => table.setPageIndex(i)}
          isActive={table.state.pagination.pageIndex === i}
        >
          {i + 1}
        </PaginationLink>
      </PaginationItem>
    ));
  }, [table.getPageCount(), table.state.pagination.pageIndex]);

  return (
    <div className="flex flex-col gap-2 h-full overflow-y-auto dataframe">
      <div className="rounded-md border overflow-y-auto">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <TableHead key={header.id}>
                    {header.isPlaceholder
                      ? null
                      : flexRender(
                          header.column.columnDef.header,
                          header.getContext()
                        )}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows?.length ? (
              table.getRowModel().rows.map((row) => (
                <TableRow key={row.id}>
                  {row.getAllCells().map((cell) => (
                    <TableCell key={cell.id}>
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext()
                      )}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell
                  colSpan={columns.length}
                  className="h-24 text-center"
                >
                  {t('components.dataframe.noResults', {
                    defaultValue: 'No results.'
                  })}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
      <Pagination>
        <PaginationContent className="ml-auto">
          <PaginationItem>
            <PaginationPrevious
              onClick={() => table.previousPage()}
              className={
                !table.getCanPreviousPage()
                  ? 'pointer-events-none opacity-50'
                  : 'cursor-pointer'
              }
            />
          </PaginationItem>
          {renderPaginationItems()}
          <PaginationItem>
            <PaginationNext
              onClick={() => table.nextPage()}
              className={
                !table.getCanNextPage()
                  ? 'pointer-events-none opacity-50'
                  : 'cursor-pointer'
              }
            />
          </PaginationItem>
        </PaginationContent>
      </Pagination>
    </div>
  );
};

function DataframeElement({ element }: { element: IDataframeElement }) {
  const { data, isLoading, error } = useFetch(element.url || null);

  const jsonData = useMemo(() => {
    if (data) return JSON.parse(data);
  }, [data]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full w-full bg-muted">
        <Loader />
      </div>
    );
  }

  if (error) {
    return <Alert variant="error">{error.message}</Alert>;
  }

  return <_DataframeElement data={jsonData} />;
}

export default DataframeElement;
