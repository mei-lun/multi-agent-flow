import { Navigate, Route, Routes } from "react-router-dom";
import { CapabilitiesPage, DashboardPage, InboxPage, LoginPage, ModelPage, ProjectDetailPage, ProjectsPage, ReviewPage, RunPage, SettingsPage, WorkflowPage } from "./App";

export function AppRouter() {
  return <Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route path="/" element={<Navigate to="/dashboard" replace />} />
    <Route path="/dashboard" element={<DashboardPage />} />
    <Route path="/projects" element={<ProjectsPage />} />
    <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
    <Route path="/models" element={<ModelPage />} />
    <Route path="/capabilities" element={<CapabilitiesPage />} />
    <Route path="/workflows" element={<WorkflowPage />} />
    <Route path="/workflows/:workflowId/versions/:versionId" element={<WorkflowPage />} />
    <Route path="/runs/:runId" element={<RunPage />} />
    <Route path="/runs/:runId/review" element={<ReviewPage />} />
    <Route path="/runs/:runId/artifacts/:artifactId" element={<ReviewPage />} />
    <Route path="/inbox" element={<InboxPage />} />
    <Route path="/settings" element={<SettingsPage />} />
    <Route path="*" element={<Navigate to="/dashboard" replace />} />
  </Routes>;
}
