from __future__ import annotations

from webcam_discovery.models.planner import PlannerPlan
from webcam_discovery.skills.target_resolution import TargetResolutionResult, TargetResolutionSkill


class TargetResolutionAgent:
    def __init__(self) -> None:
        self.skill = TargetResolutionSkill()

    def resolve(self, user_query: str, planner_plan: PlannerPlan) -> TargetResolutionResult:
        planner_locations = planner_plan.target_locations or planner_plan.parsed_intent.geography
        return self.skill.resolve(user_query=user_query, planner_locations=planner_locations)
