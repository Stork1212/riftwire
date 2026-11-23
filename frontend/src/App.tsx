import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { EmptyState, Panel } from "./components/ui";
import { ImportPage } from "./pages/ImportPage";
import { OverviewPage } from "./pages/OverviewPage";
import { QueuePage } from "./pages/QueuePage";
import { RecordingDetailPage } from "./pages/RecordingDetailPage";
import { RecordingsPage } from "./pages/RecordingsPage";
import { SystemPage } from "./pages/SystemPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<OverviewPage />} />
        <Route path="recordings" element={<RecordingsPage />} />
        <Route path="recordings/:id" element={<RecordingDetailPage />} />
        <Route path="import" element={<ImportPage />} />
        <Route path="queue" element={<QueuePage />} />
        <Route path="system" element={<SystemPage />} />
        <Route path="*" element={<Panel><EmptyState title="Page not found" /></Panel>} />
      </Route>
    </Routes>
  );
}
