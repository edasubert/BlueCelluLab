"""Layer to abstract the circuit access functionality from the rest of BGLibPy."""

from __future__ import annotations
from functools import lru_cache
import hashlib
import os
from pathlib import Path
from typing import Optional, Protocol
import warnings

from bluepy_configfile.configfile import BlueConfig
import bluepy
from bluepy.enums import Cell
from bluepy.impl.connectome_sonata import SonataConnectome
from bluepysnap.bbp import Cell as SnapCell
from bluepysnap.circuit_ids import CircuitNodeId, CircuitEdgeIds
from bluepysnap.exceptions import BluepySnapError
from bluepysnap import Circuit as SnapCircuit
import pandas as pd
from pydantic import Extra
from pydantic.dataclasses import dataclass

from bglibpy import lazy_printv, circuit, neuron
from bglibpy.circuit import CellId, SynapseProperty
from bglibpy.circuit.config import BluepySimulationConfig, SimulationConfig, SonataSimulationConfig
from bglibpy.circuit.synapse_properties import (
    properties_from_bluepy,
    properties_from_snap,
    properties_to_bluepy,
    properties_to_snap,
)
from bglibpy.exceptions import BGLibPyError


@dataclass(config=dict(extra=Extra.forbid))
class EmodelProperties:
    threshold_current: float
    holding_current: float
    ais_scaler: Optional[float] = None


class CircuitAccess(Protocol):
    """Protocol that defines the circuit access layer."""

    config: SimulationConfig

    @property
    def available_cell_properties(self) -> set:
        raise NotImplementedError

    def get_emodel_properties(self, cell_id: CellId) -> Optional[EmodelProperties]:
        raise NotImplementedError

    def get_template_format(self) -> Optional[str]:
        raise NotImplementedError

    def get_cell_properties(
        self, cell_id: CellId, properties: list[str] | str
    ) -> pd.Series:
        raise NotImplementedError

    def get_population_ids(
        self, edge_name: str
    ) -> tuple[int, int]:
        raise NotImplementedError

    def extract_synapses(
        self, cell_id: CellId, properties: list, projections: Optional[list[str] | str]
    ) -> pd.DataFrame:
        raise NotImplementedError

    def target_contains_cell(self, target: str, cell_id: CellId) -> bool:
        raise NotImplementedError

    def is_valid_group(self, group: str) -> bool:
        raise NotImplementedError

    def get_target_cell_ids(self, target: str) -> set[CellId]:
        raise NotImplementedError

    def fetch_cell_info(self, cell_id: CellId) -> pd.Series:
        raise NotImplementedError

    def fetch_mini_frequencies(self, cell_id: CellId) -> tuple:
        raise NotImplementedError

    @property
    def node_properties_available(self) -> bool:
        raise NotImplementedError

    def get_gids_of_mtypes(self, mtypes: list[str]) -> set[CellId]:
        raise NotImplementedError

    def get_cell_ids_of_targets(self, targets: list[str]) -> set[CellId]:
        raise NotImplementedError

    def morph_filepath(self, cell_id: CellId) -> str:
        raise NotImplementedError

    def emodel_path(self, cell_id: CellId) -> str:
        raise NotImplementedError


class BluepyCircuitAccess:
    """Bluepy implementation of CircuitAccess protocol."""

    def __init__(self, simulation_config: str | Path | BlueConfig | BluepySimulationConfig) -> None:
        """Initialize bluepy circuit object."""
        if isinstance(simulation_config, Path):
            simulation_config = str(simulation_config)
        if isinstance(simulation_config, str) and not Path(simulation_config).exists():
            raise FileNotFoundError(
                f"Circuit config file {simulation_config} not found.")

        # to allow the usage of SimulationConfig outside of Ssim
        if isinstance(simulation_config, BluepySimulationConfig):
            simulation_config = simulation_config.impl

        self._bluepy_sim = bluepy.Simulation(simulation_config)
        self._bluepy_circuit = self._bluepy_sim.circuit
        self.config = BluepySimulationConfig(
            self._bluepy_sim.config)

    @property
    def available_cell_properties(self) -> set:
        """Retrieve the available properties from connectome."""
        return self._bluepy_circuit.cells.available_properties

    def _get_emodel_info(self, gid: int) -> dict:
        """Return the emodel info for a gid."""
        return self._bluepy_circuit.emodels.get_mecombo_info(gid)

    def get_emodel_properties(self, cell_id: CellId) -> Optional[EmodelProperties]:
        """Get emodel_properties either from node properties or mecombo tsv."""
        gid = cell_id.id
        if self.use_mecombo_tsv:
            emodel_info = self._get_emodel_info(gid)
            emodel_properties = EmodelProperties(
                threshold_current=emodel_info["threshold_current"],
                holding_current=emodel_info["holding_current"],
                ais_scaler=None
            )
        elif self.node_properties_available:
            cell_properties = self.get_cell_properties(
                cell_id,
                properties=[
                    "@dynamics:threshold_current",
                    "@dynamics:holding_current",
                ],
            )
            emodel_properties = EmodelProperties(
                threshold_current=float(cell_properties["@dynamics:threshold_current"]),
                holding_current=float(cell_properties["@dynamics:holding_current"]),
                ais_scaler=None
            )
        else:  # old circuits
            return None

        if "@dynamics:AIS_scaler" in self.available_cell_properties:
            emodel_properties.ais_scaler = float(self.get_cell_properties(
                cell_id, properties=["@dynamics:AIS_scaler"])["@dynamics:AIS_scaler"])

        return emodel_properties

    def get_template_format(self) -> Optional[str]:
        """Return the template format."""
        if "@dynamics:AIS_scaler" in self.available_cell_properties:
            return 'v6_ais_scaler'
        elif self.use_mecombo_tsv or self.node_properties_available:
            return 'v6'
        else:
            return None

    def get_cell_properties(
        self, cell_id: CellId, properties: list[str] | str
    ) -> pd.Series:
        """Get a property of a cell."""
        gid = cell_id.id
        if isinstance(properties, str):
            properties = [properties]
        return self._bluepy_circuit.cells.get(gid, properties=properties)

    def get_population_ids(
        self, edge_name: str
    ) -> tuple[int, int]:
        """Retrieve the population ids of a projection."""
        projection = edge_name
        if projection in self.config.get_all_projection_names():
            if "PopulationID" in self.config.impl[f"Projection_{projection}"]:
                source_popid = int(self.config.impl[f"Projection_{projection}"]["PopulationID"])
            else:
                source_popid = 0
        else:
            source_popid = 0
        # ATM hard coded in neurodamus, commit: cd26654
        target_popid = 0
        return source_popid, target_popid

    def _get_connectomes_dict(self, projections: Optional[list[str] | str]) -> dict:
        """Get the connectomes dictionary indexed by projections or connectome ids."""
        if isinstance(projections, str):
            projections = [projections]

        connectomes = {'': self._bluepy_circuit.connectome}
        if projections is not None:
            proj_conns = {p: self._bluepy_circuit.projection(p) for p in projections}
            connectomes.update(proj_conns)

        return connectomes

    def extract_synapses(
        self, cell_id: CellId, properties: list, projections: Optional[list[str] | str]
    ) -> pd.DataFrame:
        """Extract the synapses of a cell.

        Returns:
            synapses dataframes indexed by projection, edge and synapse ids
        """
        gid = cell_id.id
        connectomes = self._get_connectomes_dict(projections)

        all_synapses: list[pd.DataFrame] = []
        for proj_name, connectome in connectomes.items():
            connectome_properties = list(properties)

            # older circuit don't have these properties
            for test_property in [SynapseProperty.U_HILL_COEFFICIENT,
                                  SynapseProperty.CONDUCTANCE_RATIO,
                                  SynapseProperty.NRRP]:
                if test_property.to_bluepy() not in connectome.available_properties:
                    connectome_properties.remove(test_property)
                    lazy_printv(f'WARNING: {test_property} not found, disabling', 50)

            if isinstance(connectome._impl, SonataConnectome):
                lazy_printv('Using sonata style synapse file, not nrn.h5', 50)
                # load 'afferent_section_pos' instead of '_POST_DISTANCE'
                if 'afferent_section_pos' in connectome.available_properties:
                    connectome_properties[
                        connectome_properties.index(SynapseProperty.POST_SEGMENT_OFFSET)
                    ] = 'afferent_section_pos'

                connectome_properties = properties_to_bluepy(connectome_properties)
                synapses = connectome.afferent_synapses(
                    gid, properties=connectome_properties
                )
                synapses.columns = properties_from_bluepy(synapses.columns)
            else:
                connectome_properties = properties_to_bluepy(connectome_properties)
                synapses = connectome.afferent_synapses(
                    gid, properties=connectome_properties
                )
                synapses.columns = properties_from_bluepy(synapses.columns)

            synapses = synapses.reset_index(drop=True)
            synapses.index = pd.MultiIndex.from_tuples(
                [(proj_name, x) for x in synapses.index],
                names=["proj_id", "synapse_id"])

            lazy_printv('Retrieving a total of {n_syn} synapses for set {syn_set}',
                        5, n_syn=synapses.shape[0], syn_set=proj_name)

            all_synapses.append(synapses)

        result = pd.concat(all_synapses)

        # io/synapse_reader.py:_patch_delay_fp_inaccuracies from
        # py-neurodamus
        dt = neuron.h.dt
        result[SynapseProperty.AXONAL_DELAY] = (
            result[SynapseProperty.AXONAL_DELAY] / dt + 1e-5
        ).astype('i4') * dt

        if SynapseProperty.NRRP in result:
            circuit.validate.check_nrrp_value(result)

        proj_ids: list[str] = result.index.get_level_values(0).tolist()
        pop_ids = [
            self.get_population_ids(proj_id)
            for proj_id in proj_ids
        ]
        source_popid, target_popid = zip(*pop_ids)

        result = result.assign(
            source_popid=source_popid, target_popid=target_popid
        )

        if result.empty:
            lazy_printv('No synapses found', 5)
        else:
            lazy_printv('Found a total of {n_syn_sets} synapse sets',
                        5, n_syn_sets=len(result))

        return result

    @property
    def use_mecombo_tsv(self) -> bool:
        """Property that decides whether to use mecombo_tsv."""
        _use_mecombo_tsv = False
        if self.node_properties_available:
            _use_mecombo_tsv = False
        elif 'MEComboInfoFile' in self.config.impl.Run:
            _use_mecombo_tsv = True
        return _use_mecombo_tsv

    def target_contains_cell(self, target: str, cell_id: CellId) -> bool:
        """Check if target contains the cell or target is the cell."""
        return self._is_cell_target(target, cell_id) or self._target_has_gid(target, cell_id)

    @staticmethod
    def _is_cell_target(target: str, cell_id: CellId) -> bool:
        """Check if target is a cell."""
        return target == f"a{cell_id.id}"

    @lru_cache(maxsize=1000)
    def is_valid_group(self, group: str) -> bool:
        """Check if target is a group of cells."""
        return group in self._bluepy_circuit.cells.targets

    @lru_cache(maxsize=16)
    def get_target_cell_ids(self, target: str) -> set[CellId]:
        """Return GIDs in target as a set."""
        ids = self._bluepy_circuit.cells.ids(target)
        return {CellId("", id) for id in ids}

    @lru_cache(maxsize=1000)
    def _target_has_gid(self, target: str, cell_id: CellId) -> bool:
        """Checks if target has the gid."""
        return cell_id in self.get_target_cell_ids(target)

    @lru_cache(maxsize=100)
    def fetch_cell_info(self, cell_id: CellId) -> pd.Series:
        """Fetch bluepy cell info of a gid"""
        gid = cell_id.id
        if gid in self._bluepy_circuit.cells.ids():
            return self._bluepy_circuit.cells.get(gid)
        else:
            raise BGLibPyError(f"Gid {gid} not found in circuit")

    def _fetch_mecombo_name(self, cell_id: CellId) -> str:
        """Fetch mecombo name for a certain gid."""
        cell_info = self.fetch_cell_info(cell_id)
        if self.node_properties_available:
            me_combo = str(cell_info['model_template'])
            me_combo = me_combo.split('hoc:')[1]
        else:
            me_combo = str(cell_info['me_combo'])
        return me_combo

    def _fetch_emodel_name(self, cell_id: CellId) -> str:
        """Get the emodel path of a gid."""
        me_combo = self._fetch_mecombo_name(cell_id)
        if self.use_mecombo_tsv:
            gid = cell_id.id
            emodel_name = self._bluepy_circuit.emodels.get_mecombo_info(gid)["emodel"]
        else:
            emodel_name = me_combo

        return emodel_name

    def fetch_mini_frequencies(self, cell_id: CellId) -> tuple:
        """Get inhibitory frequency of gid."""
        cell_info = self.fetch_cell_info(cell_id)
        # mvd uses inh_mini_frequency, sonata uses inh-mini_frequency
        inh_keys = ("inh-mini_frequency", "inh_mini_frequency")
        exc_keys = ("exc-mini_frequency", "exc_mini_frequency")

        inh_mini_frequency, exc_mini_frequency = None, None
        for inh_key in inh_keys:
            if inh_key in cell_info:
                inh_mini_frequency = cell_info[inh_key]
        for exc_key in exc_keys:
            if exc_key in cell_info:
                exc_mini_frequency = cell_info[exc_key]

        return exc_mini_frequency, inh_mini_frequency

    @property
    def node_properties_available(self) -> bool:
        """Checks if the node properties are available and can be used."""
        node_props = {
            "@dynamics:holding_current",
            "@dynamics:threshold_current",
            "model_template",
        }

        return node_props.issubset(self._bluepy_circuit.cells.available_properties)

    def get_gids_of_mtypes(self, mtypes: list[str]) -> set[CellId]:
        """Returns all the gids belonging to one of the input mtypes."""
        gids = set()
        for mtype in mtypes:
            ids = self._bluepy_circuit.cells.ids({Cell.MTYPE: mtype})
            gids |= {CellId("", x) for x in ids}

        return gids

    def get_cell_ids_of_targets(self, targets: list[str]) -> set[CellId]:
        """Return all the gids belonging to one of the input targets."""
        cell_ids = set()
        for target in targets:
            cell_ids |= self.get_target_cell_ids(target)
        return cell_ids

    def morph_filepath(self, cell_id: CellId) -> str:
        return self._bluepy_circuit.morph.get_filepath(cell_id.id, "ascii")

    def emodel_path(self, cell_id: CellId) -> str:
        return os.path.join(self._emodels_dir, f"{self._fetch_emodel_name(cell_id)}.hoc")

    @property
    def _emodels_dir(self) -> str:
        return self.config.impl.Run['METypePath']


class SonataCircuitAccess:
    """Sonata implementation of CircuitAccess protocol."""

    def __init__(self, simulation_config: str | Path | SimulationConfig) -> None:
        """Initialize SonataCircuitAccess object."""
        if isinstance(simulation_config, (str, Path)) and not Path(simulation_config).exists():
            raise FileNotFoundError(f"Circuit config file {simulation_config} not found.")

        if isinstance(simulation_config, SonataSimulationConfig):
            self.config: SimulationConfig = simulation_config
        else:
            self.config = SonataSimulationConfig(simulation_config)
        circuit_config = self.config.impl.config["network"]
        self._circuit = SnapCircuit(circuit_config)

    @property
    def available_cell_properties(self) -> set:
        return self._circuit.nodes.property_names

    def get_emodel_properties(self, cell_id: CellId) -> Optional[EmodelProperties]:
        cell_properties = self._circuit.nodes[cell_id.population_name].get(cell_id.id)
        if "@dynamics:AIS_scaler" in cell_properties:
            ais_scaler = cell_properties["@dynamics:AIS_scaler"]
        else:
            ais_scaler = None
        return EmodelProperties(
            cell_properties["@dynamics:threshold_current"],
            cell_properties["@dynamics:holding_current"],
            ais_scaler,
        )

    def get_template_format(self) -> Optional[str]:
        if "@dynamics:AIS_scaler" in self.available_cell_properties:
            return 'v6_ais_scaler'
        else:
            return 'v6'

    def get_cell_properties(
        self, cell_id: CellId, properties: list[str] | str
    ) -> pd.Series:
        if isinstance(properties, str):
            properties = [properties]
        return self._circuit.nodes[cell_id.population_name].get(
            cell_id.id, properties=properties
        )

    @staticmethod
    def _compute_pop_ids(source: str, target: str) -> tuple[int, int]:
        """Compute the population ids from the population names."""
        def make_id(node_pop: str) -> int:
            pop_hash = hashlib.md5(node_pop.encode()).digest()
            return ((pop_hash[1] & 0x0f) << 8) + pop_hash[0]  # id: 12bit hash

        source_popid = make_id(source)
        target_popid = make_id(target)
        return source_popid, target_popid

    def get_population_ids(
        self, edge_name: str
    ) -> tuple[int, int]:
        source, target = edge_name.split("__")[0:2]
        source_popid, target_popid = self._compute_pop_ids(source, target)
        return source_popid, target_popid

    def extract_synapses(
        self, cell_id: CellId, properties: list, projections: Optional[list[str] | str]
    ) -> pd.DataFrame:
        """Extract the synapses. If projections is None, all the synapses are extracted."""
        snap_node_id = CircuitNodeId(cell_id.population_name, cell_id.id)
        edges = self._circuit.edges
        # select edges that are in the projections, if there are projections
        if projections is None or len(projections) == 0:
            edge_names = [x for x in edges]
        elif isinstance(projections, str):
            edge_names = [x for x in edges if edges[x].source.name == projections]
        else:
            edge_names = [x for x in edges if edges[x].source.name in projections]

        all_synapses_dfs: list[pd.DataFrame] = []
        for edge_name in edge_names:
            edge = edges[edge_name]
            afferent_edges: CircuitEdgeIds = edge.afferent_edges(snap_node_id)
            if len(afferent_edges) != 0:
                edge_properties = list(properties)  # copy, it'll be modified

                # remove optional properties if they are not present
                for optional_property in [SynapseProperty.U_HILL_COEFFICIENT,
                                          SynapseProperty.CONDUCTANCE_RATIO]:
                    if optional_property.to_snap() not in edge.property_names:
                        edge_properties.remove(optional_property)

                snap_properties = properties_to_snap(edge_properties)
                synapses: pd.DataFrame = edge.get(afferent_edges, snap_properties)
                column_names = list(synapses.columns)
                synapses.columns = pd.Index(properties_from_snap(column_names))

                # make multiindex
                synapses = synapses.reset_index(drop=True)
                synapses.index = pd.MultiIndex.from_tuples(
                    zip([edge_name] * len(synapses), synapses.index),
                    names=["edge_name", "synapse_id"],
                )

                # add source_population_name as a column
                source_population_name = edges[edge_name].source.name
                synapses["source_population_name"] = source_population_name

                # py-neurodamus
                dt = neuron.h.dt
                synapses[SynapseProperty.AXONAL_DELAY] = (
                    synapses[SynapseProperty.AXONAL_DELAY] / dt + 1e-5
                ).astype('i4') * dt

                if SynapseProperty.NRRP in synapses:
                    circuit.validate.check_nrrp_value(synapses)

                source_popid, target_popid = self.get_population_ids(edge_name)
                synapses = synapses.assign(
                    source_popid=source_popid, target_popid=target_popid
                )

                all_synapses_dfs.append(synapses)

        if len(all_synapses_dfs) == 0:
            return pd.DataFrame()
        else:
            return pd.concat(all_synapses_dfs)  # outer join that creates NaNs

    def target_contains_cell(self, target: str, cell_id: CellId) -> bool:
        return cell_id in self.get_target_cell_ids(target)

    @lru_cache(maxsize=1000)
    def is_valid_group(self, group: str) -> bool:
        return group in self._circuit.node_sets

    @lru_cache(maxsize=16)
    def get_target_cell_ids(self, target: str) -> set[CellId]:
        ids = self._circuit.nodes.ids(target)
        return {CellId(x.population, x.id) for x in ids}

    @lru_cache(maxsize=100)
    def fetch_cell_info(self, cell_id: CellId) -> pd.Series:
        return self._circuit.nodes[cell_id.population_name].get(cell_id.id)

    def fetch_mini_frequencies(self, cell_id: CellId) -> tuple:
        cell_info = self.fetch_cell_info(cell_id)
        exc_mini_frequency = cell_info['exc-mini_frequency'] \
            if 'exc-mini_frequency' in cell_info else None
        inh_mini_frequency = cell_info['inh-mini_frequency'] \
            if 'inh-mini_frequency' in cell_info else None
        return exc_mini_frequency, inh_mini_frequency

    @property
    def node_properties_available(self) -> bool:
        return True

    def get_gids_of_mtypes(self, mtypes: list[str]) -> set[CellId]:
        all_cell_ids = set()
        all_population_names: list[str] = list(self._circuit.nodes)
        for population_name in all_population_names:
            try:
                cell_ids = self._circuit.nodes[population_name].ids(
                    {SnapCell.MTYPE: mtypes})
            except BluepySnapError:
                continue
            all_cell_ids |= {CellId(population_name, id) for id in cell_ids}
        return all_cell_ids

    def get_cell_ids_of_targets(self, targets: list[str]) -> set[CellId]:
        cell_ids = set()
        for target in targets:
            cell_ids |= self.get_target_cell_ids(target)
        return cell_ids

    def morph_filepath(self, cell_id: CellId) -> str:
        """Returns the .asc morphology path from 'alternate_morphologies'."""
        node_population = self._circuit.nodes[cell_id.population_name]
        try:  # if asc defined in alternate morphology
            return str(node_population.morph.get_filepath(cell_id.id, extension="asc"))
        except BluepySnapError as e:
            warnings.warn(str(e))
            return str(node_population.morph.get_filepath(cell_id.id))

    def emodel_path(self, cell_id: CellId) -> str:
        node_population = self._circuit.nodes[cell_id.population_name]
        return str(node_population.models.get_filepath(cell_id.id))
