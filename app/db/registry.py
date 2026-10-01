"""Import every model module so ``Base.metadata`` is complete (for Alembic)."""

from app.budgets import models as budgets_models
from app.experiments import models as experiments_models
from app.ips import models as ips_models
from app.production import models as production_models
from app.publishing import models as publishing_models
from app.quality import models as quality_models

__all__ = [
    "budgets_models",
    "experiments_models",
    "ips_models",
    "production_models",
    "publishing_models",
    "quality_models",
]
