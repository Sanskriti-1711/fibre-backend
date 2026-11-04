from django.test import TestCase

from projects.models import Feature, Project
from supervision.decisions import record_approval_decisions, record_redo_decisions
from supervision.models import FeatureDecisionRecord


class ReviewLedgerTests(TestCase):
    """Every decision is recorded, linked to the project and the feature."""

    def setUp(self):
        self.project = Project.objects.create(name='Ledger project')
        self.feature = Feature.objects.create(
            project=self.project,
            layer_name='distribution_ducts',
            layer_id='distribution_ducts',
            properties={'length': 10, 'depth_mm': 600},
            field_measurements={
                'length': {'value': 10, 'unit': 'm'},
                'depth_mm': {'value': 600, 'unit': 'mm'},
            },
        )

    def test_approval_writes_a_project_linked_record(self):
        result = record_approval_decisions([self.feature], notes='checked against design')

        self.assertEqual(len(result['records']), 1)
        self.assertFalse(result['deviations'])

        record = FeatureDecisionRecord.objects.get()
        self.assertEqual(record.project_id, self.project.id)
        self.assertEqual(record.feature_id, self.feature.id)
        self.assertEqual(record.decision, FeatureDecisionRecord.DECISION_APPROVED)
        self.assertEqual(record.reason, 'checked against design')
        self.assertIn('fields', record.deviation)

    def test_redo_writes_a_record_with_the_comparison(self):
        records = record_redo_decisions([self.feature], reason='chamber 2 m off alignment')

        self.assertEqual(len(records), 1)
        record = FeatureDecisionRecord.objects.get()
        self.assertEqual(record.project_id, self.project.id)
        self.assertEqual(record.decision, FeatureDecisionRecord.DECISION_REDO)
        self.assertEqual(record.reason, 'chamber 2 m off alignment')
        self.assertIn('fields', record.deviation)

    def test_each_decision_adds_a_row_rather_than_replacing_the_last(self):
        record_approval_decisions([self.feature], notes='first pass')
        record_redo_decisions([self.feature], reason='re-measure')
        record_approval_decisions([self.feature], notes='second pass')

        self.assertEqual(FeatureDecisionRecord.objects.filter(feature=self.feature).count(), 3)
        self.assertEqual(
            list(
                FeatureDecisionRecord.objects.filter(feature=self.feature).values_list(
                    'decision', flat=True
                )
            ),
            [
                FeatureDecisionRecord.DECISION_APPROVED,
                FeatureDecisionRecord.DECISION_REDO,
                FeatureDecisionRecord.DECISION_APPROVED,
            ],
        )
