import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { EmptyState, Panel } from "./components/ui";
import { CatalogPage } from "./pages/CatalogPage";
import { CreatorsPage } from "./pages/CreatorsPage";
import { ImportPage } from "./pages/ImportPage";
import { OverviewPage } from "./pages/OverviewPage";
import { QueuePage } from "./pages/QueuePage";
import { RecordingDetailPage } from "./pages/RecordingDetailPage";
import { RecordingsPage } from "./pages/RecordingsPage";
import { SourceDetailPage } from "./pages/SourceDetailPage";
import { SourcesPage } from "./pages/SourcesPage";
import { StoragePage } from "./pages/StoragePage";
import { SystemPage } from "./pages/SystemPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<OverviewPage />} />
        <Route path="recordings" element={<RecordingsPage />} />
        <Route path="recordings/:id" element={<RecordingDetailPage />} />
        <Route path="import" element={<ImportPage />} />
        <Route path="sources" element={<SourcesPage />} />
        <Route path="sources/:id" element={<SourceDetailPage />} />
        <Route path="creators" element={<CreatorsPage />} />
        <Route path="catalog" element={<CatalogPage />} />
        <Route path="queue" element={<QueuePage />} />
        <Route path="storage" element={<StoragePage />} />
        <Route path="system" element={<SystemPage />} />
        <Route path="*" element={<Panel><EmptyState title="Page not found" /></Panel>} />
      </Route>
    </Routes>
  );
}
