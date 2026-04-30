/**
 * Static info card with no capabilities.
 *
 * Pattern: presentational. The card renders content and never reaches back
 * to the host. Good for summaries, status snapshots, and "here is the answer"
 * artifacts the user is meant to read but not interact with.
 *
 * Capabilities: [] (none — purely visual)
 * Imports: React; Card, Stack, Text, Field from 'canvas-primitives'.
 */

import React from 'react';
import { Card, Stack, Text, Field } from 'canvas-primitives';

export default function StaticInfoCard() {
  return (
    <Card title="Quarterly summary">
      <Stack gap={12}>
        <Field label="Quarter">Q3 2025</Field>
        <Field label="Revenue">$4.2M</Field>
        <Field label="Growth">+18% YoY</Field>
        <Text muted>
          Generated from the closing financial pack on 2025-10-15.
        </Text>
      </Stack>
    </Card>
  );
}
