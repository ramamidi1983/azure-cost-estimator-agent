import unittest
from unittest.mock import patch

import pandas as pd

import estimator as E


VM_RATE = {"payg": 0.20, "sp1y": 0.12, "sp3y": 0.09}
ACA_RATES = {
    "ded_vcpu_hr": 0.10,
    "ded_vcpu_hr_1y": 0.08,
    "ded_mem_hr": 0.01,
    "ded_mem_hr_1y": 0.008,
    "cons_vcpu_sec": 0.00002,
    "cons_mem_sec": 0.000003,
}


class ContainerOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.inventory = pd.DataFrame([
            {
                "name": "prod-api",
                "environment": "Prod",
                "role": "application",
                "disposition": "Refactor",
                "vcpu": 4,
                "memory_gb": 16,
                "quantity": 1,
                "hours": 730,
            },
            {
                "name": "test-api",
                "environment": "Test",
                "role": "application",
                "disposition": "Modernize",
                "vcpu": 2,
                "memory_gb": 8,
                "quantity": 1,
                "hours": 730,
            },
        ])
        self.pricing = [
            patch("estimator.P.vm_price", return_value=VM_RATE),
            patch("estimator.P.aks_cluster_fee", return_value=0.10),
            patch("estimator.P.aca_rates", return_value=ACA_RATES),
            patch("estimator.P.appservice_price", return_value=VM_RATE),
        ]
        for item in self.pricing:
            item.start()

    def tearDown(self):
        for item in reversed(self.pricing):
            item.stop()

    def test_estimate_adds_one_shared_cluster_per_active_environment(self):
        lines, _ = E.estimate(
            self.inventory,
            container_opts={"pool_aks": True, "optimize_aca": True},
        )
        shared = lines[lines["sku"].astype(str).str.startswith("AKS Shared Cluster")]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared.iloc[0]["environment"], "Prod")
        aca = lines[lines["name"] == "test-api"].iloc[0]
        self.assertIn("active", aca["rate_basis"])
        self.assertEqual(aca["sku"], "ACA Consumption")

    def test_modernization_exposes_reusable_optimized_options(self):
        modern = E.modernization(self.inventory)
        self.assertIn("Containerize (AKS - Shared)", modern.columns)
        self.assertIn("Modernize (Container Apps - Optimized)", modern.columns)
        self.assertIn("Best Container Option", modern.columns)
        prod = modern[modern["name"] == "prod-api"].iloc[0]
        self.assertLess(
            prod["Containerize (AKS - Shared)"],
            prod["Containerize (AKS - Per App)"],
        )

    def test_shared_cluster_control_plane_is_not_charged_as_asr_instance(self):
        lines, _ = E.estimate(
            self.inventory,
            resiliency=True,
            container_opts={"pool_aks": True},
        )
        with_pool = E.build_summary(lines, resiliency=True)
        without_platform = E.build_summary(
            lines[lines["role"] != "AKS platform"],
            resiliency=True,
        )
        pooled_resiliency = with_pool.loc[
            with_pool["area"] == "Resiliency Add-In", "monthly"
        ].iloc[0]
        base_resiliency = without_platform.loc[
            without_platform["area"] == "Resiliency Add-In", "monthly"
        ].iloc[0]
        self.assertEqual(pooled_resiliency, base_resiliency)


if __name__ == "__main__":
    unittest.main()
