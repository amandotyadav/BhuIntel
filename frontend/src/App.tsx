import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { Loading } from "./components/ui";
import { AcquisitionPage } from "./pages/AcquisitionPage";
import { AuditPage } from "./pages/AuditPage";
import { ChangesPage } from "./pages/ChangesPage";
import { DatasetsPage } from "./pages/DatasetsPage";
import { IntegrationPage } from "./pages/IntegrationPage";
import { JobsPage } from "./pages/JobsPage";
import { LoginPage } from "./pages/LoginPage";
import { MapPage } from "./pages/MapPage";
import { MatchingPage } from "./pages/MatchingPage";
import { ModelsPage } from "./pages/ModelsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { ProjectLayout } from "./pages/ProjectLayout";
import { ProjectsPage } from "./pages/ProjectsPage";
import { ReviewPage } from "./pages/ReviewPage";

export function App() {
  const { user, loading } = useAuth();
  if (loading) return <Loading label="Restoring session…" />;
  if (!user) {
    return (
      <Routes>
        <Route path="*" element={<LoginPage />} />
      </Routes>
    );
  }
  return (
    <Routes>
      <Route path="/" element={<ProjectsPage />} />
      <Route path="/projects/:projectId" element={<ProjectLayout />}>
        <Route index element={<OverviewPage />} />
        <Route path="map" element={<MapPage />} />
        <Route path="datasets" element={<DatasetsPage />} />
        <Route path="matching" element={<MatchingPage />} />
        <Route path="review" element={<ReviewPage />} />
        <Route path="changes" element={<ChangesPage />} />
        <Route path="models" element={<ModelsPage />} />
        <Route path="integration" element={<IntegrationPage />} />
        <Route path="acquisition" element={<AcquisitionPage />} />
        <Route path="jobs" element={<JobsPage />} />
        <Route path="audit" element={<AuditPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
