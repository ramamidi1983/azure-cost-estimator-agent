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
    "cons_vcpu_sec_1y": 0.000017,
    "cons_vcpu_sec_3y": 0.000016,
    "cons_mem_sec": 0.000003,
    "cons_mem_sec_1y": 0.00000255,
    "cons_mem_sec_3y": 0.00000249,
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
            patch("estimator.P.azure_files_lrs_rate", return_value=0.06),
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
        self.assertIn("Selected Container Option", modern.columns)
        prod = modern[modern["name"] == "prod-api"].iloc[0]
        self.assertLess(
            prod["Containerize (AKS - Shared)"],
            prod["Containerize (AKS - Per App)"],
        )

    def test_modernization_selections_follow_checkboxes(self):
        optimized = E.modernization(
            self.inventory,
            container_opts={"pool_aks": True, "optimize_aca": True},
        )
        unoptimized = E.modernization(
            self.inventory,
            container_opts={"pool_aks": False, "optimize_aca": False},
        )
        opt_total = optimized[optimized["name"] == "TOTAL"].iloc[0]
        base_total = unoptimized[unoptimized["name"] == "TOTAL"].iloc[0]
        self.assertLess(
            opt_total["Selected AKS Scenario"],
            base_total["Selected AKS Scenario"],
        )
        self.assertLess(
            opt_total["Selected Container Apps Scenario"],
            base_total["Selected Container Apps Scenario"],
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

    def test_container_apps_consumption_uses_selected_term_rate(self):
        payg = E.cost_aca(2, 4, 200, 1, "payg", "eastus", profile="consumption")
        one_year = E.cost_aca(2, 4, 200, 1, "1y", "eastus", profile="consumption")
        three_year = E.cost_aca(2, 4, 200, 1, "3y", "eastus", profile="consumption")
        self.assertLess(one_year["monthly"], payg["monthly"])
        self.assertLess(three_year["monthly"], one_year["monthly"])
        self.assertEqual(one_year["rate_basis"], "1yr SP active-hrs")

    def test_container_strategy_retargets_rehost_apps_in_main_estimate(self):
        rehost = self.inventory.iloc[[0]].copy()
        rehost.loc[:, "disposition"] = "Rehost"
        unchanged, _ = E.estimate(rehost)
        aks, _ = E.estimate(
            rehost,
            container_opts={
                "container_strategy": "aks",
                "pool_aks": True,
                "aks_demand_factor": 0.10,
                "aks_target_utilization": 0.40,
                "aks_headroom": 0.0,
            },
        )
        aca, _ = E.estimate(
            rehost,
            container_opts={
                "container_strategy": "aca",
                "optimize_aca": True,
                "aca_prod_active_factor": 0.05,
            },
        )
        self.assertTrue((unchanged["target"] == "vm").all())
        self.assertTrue((aks["target"] == "aks").all())
        self.assertTrue((aca["target"] == "aca").all())
        self.assertNotEqual(unchanged["monthly"].sum(), aks["monthly"].sum())
        self.assertNotEqual(unchanged["monthly"].sum(), aca["monthly"].sum())

    def test_unknown_servers_are_not_automatically_containerized(self):
        unknown = pd.DataFrame([{
            "name": "server-001",
            "environment": "Prod",
            "role": "",
            "disposition": "Rehost",
            "vcpu": 4,
            "memory_gb": 16,
            "quantity": 1,
            "hours": 730,
        }])
        lines, _ = E.estimate(
            unknown,
            container_opts={"container_strategy": "aks", "pool_aks": True},
        )
        self.assertTrue((lines["target"] == "vm").all())
        self.assertFalse((lines["role"] == "AKS platform").any())

    def test_avd_prices_session_hosts_profiles_and_optional_access(self):
        avd = pd.DataFrame([{
            "name": "corporate-desktops",
            "environment": "Prod",
            "role": "virtual desktops",
            "target": "avd",
            "vcpu": 4,
            "memory_gb": 16,
            "quantity": 101,
            "users_per_host": 10,
            "profile_storage_gb": 30,
            "unit_price": 10,
            "hours": 730,
        }])
        lines, _ = E.estimate(avd, term="1y")
        self.assertEqual(set(lines["component"]), {"Compute", "Storage", "License"})
        compute = lines[lines["component"] == "Compute"].iloc[0]
        storage = lines[lines["component"] == "Storage"].iloc[0]
        license_line = lines[lines["component"] == "License"].iloc[0]
        self.assertIn("11x", compute["sku"])
        self.assertAlmostEqual(storage["monthly"], 101 * 30 * 0.06)
        self.assertAlmostEqual(license_line["monthly"], 101 * 10)


if __name__ == "__main__":
    unittest.main()
