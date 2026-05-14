/**
 * Drag-and-drop kanban board with three columns. Agent owns the column +
 * item state; the card emits `onMove` events the agent applies to its model.
 *
 * Pattern: card-controlled state with agent feedback via canvasAPI calls.
 * The card uses local React state to render the board responsively (no
 * waiting for the agent to round-trip on every drop), and reports each move
 * via `widget_message` (handled below) for the agent to persist. Initial
 * state arrives via `widget.message` so the agent decides what columns and
 * items the user sees.
 *
 * `<KanbanBoard>` lives in `canvas-primitives/heavy` (lazy-loaded chunk).
 * It shares its chunk with `<DnDList>` since both use @dnd-kit. React
 * requires the consumer to wrap lazy components in `<Suspense>`.
 *
 * Capabilities: [] (the card receives initial state via widget.message and
 *                  reports moves via canvasAPI.onMessage callback consumer)
 * Imports: React (with hooks + Suspense); Card from 'canvas-primitives';
 *          KanbanBoard from 'canvas-primitives/heavy';
 *          canvasAPI.onMessage for initial / agent-pushed state.
 */

import React, { Suspense, useEffect, useState } from 'react';
import { Card } from 'canvas-primitives';
import { KanbanBoard } from 'canvas-primitives/heavy';

declare const canvasAPI: {
  onMessage(handler: (msg: unknown) => void): () => void;
};

interface BoardItem {
  id: string;
  label: string;
}
interface BoardColumn {
  id: string;
  title: string;
  items: BoardItem[];
}

const INITIAL: BoardColumn[] = [
  { id: 'todo', title: 'To Do', items: [] },
  { id: 'doing', title: 'Doing', items: [] },
  { id: 'done', title: 'Done', items: [] },
];

export default function Kanban() {
  const [columns, setColumns] = useState<BoardColumn[]>(INITIAL);
  const [title, setTitle] = useState<string>('Kanban');

  useEffect(() => {
    return canvasAPI.onMessage((msg) => {
      const m = msg as { kind?: string; columns?: BoardColumn[]; title?: string };
      if (m.kind === 'board.refresh' && Array.isArray(m.columns)) {
        setColumns(m.columns);
        if (typeof m.title === 'string') setTitle(m.title);
      }
    });
  }, []);

  function handleMove(
    itemId: string,
    fromColumnId: string,
    toColumnId: string,
    toIndex: number,
  ): void {
    setColumns((prev) => {
      const next = prev.map((c) => ({ ...c, items: [...c.items] }));
      const fromCol = next.find((c) => c.id === fromColumnId);
      const toCol = next.find((c) => c.id === toColumnId);
      if (!fromCol || !toCol) return prev;
      const fromIndex = fromCol.items.findIndex((i) => i.id === itemId);
      if (fromIndex < 0) return prev;
      const [moved] = fromCol.items.splice(fromIndex, 1);
      toCol.items.splice(toIndex, 0, moved);
      return next;
    });
  }

  return (
    <Card title={title}>
      <Suspense fallback={<div style={{ padding: 12 }}>Loading board…</div>}>
        <KanbanBoard columns={columns} onMove={handleMove} />
      </Suspense>
    </Card>
  );
}
