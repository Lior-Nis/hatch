"""Import every model module so ``Base.metadata`` is complete (for Alembic)."""

from app.analytics import models as analytics_models
from app.budgets import models as budgets_models
from app.characters import models as characters_models
from app.evolution import models as evolution_models
from app.experiments import models as experiments_models
from app.fitness import models as fitness_models
from app.ips import models as ips_models
from app.knowledge import models as knowledge_models
from app.llm import models as llm_models
from app.production import models as production_models
from app.publishing import models as publishing_models
from app.quality import models as quality_models
from app.scheduling import models as scheduling_models

__all__ = [
    "analytics_models",
    "budgets_models",
    "characters_models",
    "evolution_models",
    "experiments_models",
    "fitness_models",
    "ips_models",
    "knowledge_models",
    "llm_models",
    "production_models",
    "publishing_models",
    "quality_models",
    "scheduling_models",
]
