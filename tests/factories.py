"""Small builders for persisted test data."""

from sqlalchemy.orm import Session

from app.experiments.fixtures import FIRST_SHORT
from app.experiments.models import Experiment
from app.experiments.service import create_experiment


def make_experiment(session: Session) -> Experiment:
    return create_experiment(session, FIRST_SHORT)
