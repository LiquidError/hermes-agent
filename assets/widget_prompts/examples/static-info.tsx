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
      <Stack gap={3}>
        <Field label="Quarter"><Text>Q3 2025</Text></Field>
        <Field label="Revenue"><Text>$4.2M</Text></Field>
        <Field label="Growth"><Text>+18% YoY</Text></Field>
        <Text variant="muted">
          Generated from the closing financial pack on 2025-10-15.
        </Text>
      </Stack>
    </Card>
  );
}
