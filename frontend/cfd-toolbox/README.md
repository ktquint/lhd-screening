# FLOW-3D CFD Toolbox for Low-Head Dam Hazard Analysis

## Start Here

This HydroShare resource provides an online training toolbox for setting up, running, and interpreting a FLOW-3D HYDRO computational fluid dynamics (CFD) model for assessing drowning potential at low-head dams. The toolbox is designed as a step-by-step learning module with example files, screenshots, model inputs, downloadable results, and post-processing materials.

The purpose of this toolbox is to guide users through the complete modeling workflow, from preparing input data to interpreting FLOW-3D simulation results.

---

## How to Use This Toolbox

1. Review the **Tool Summary** section to understand the purpose and scope of the toolbox.
2. Review **What is Needed** before opening FLOW-3D.
3. Download the input files, geometry files, boundary-condition files, and model files from the HydroShare resource content section.
4. Follow the **Step-by-Step Walkthrough in FLOW-3D**.
5. Compare your outputs with the example results provided in the **Summary of Results** section.
6. Use the downloadable files and post-processing materials to reproduce or adapt the workflow for other low-head dam sites.

---

## 1. Tool Summary

### Objective and Scope

This toolbox demonstrates how FLOW-3D can be used to set up, run, and interpret a three-dimensional CFD model for low-head dam reverse-roller and drowning-potential assessment.This manual toolbox discusses the step-by-step process for simulating the flow at low head dams in different scenario. It includes analysis of several ranges of discharge/head for which reverse roller forms, its drowning potential, flow characteristics, analyzing discharge, water surface profile, velocity distribution etc. 

The current example focuses on:

- Low-head dam hydraulic hazard analysis
- Free-surface flow simulation
- Velocity and water-surface analysis
- Reverse-roller and submerged-jump behavior
- Identification of high-velocity zones, recirculation zones, and hydraulic conditions that may contribute to public-safety risk

### Motivation

Recent advances in three-dimensional CFD modeling allow modelers to analyze reverse-roller hydraulics and, when needed, evaluate floating-body behavior using human-prototype or simplified floating-body representations. This type of modeling can support the assessment of drowning potential and can also be used to evaluate structural modifications intended to reduce reverse-roller strength.

FLOW-3D outputs also provide intuitive visualizations that can help communicate hydraulic hazards to first responders, dam owners, government officials, and the public. While several commercial and open-source CFD tools are available for hydraulic-structure modeling, FLOW-3D is widely used because it provides robust free-surface and multiphase-flow modeling capabilities for complex hydraulic conditions.

A CFD toolbox can assist dam owners and stakeholders in identifying reverse-roller hazards and evaluating mitigation alternatives. A straightforward CFD workflow allows users to assess low-head dam drowning potential in ways that are not possible using only one-dimensional or two-dimensional hydraulic models.

The toolbox can also be used to analyze structural modifications such as sills, baffle blocks, and other features that may be placed on the downstream apron or stilling floor to reduce hazardous reverse-roller behavior.

[View Figure 1: Formation of submerged jump/reverse roller](lhd.png)

**Figure 1.** Formation of a submerged jump/reverse roller near the toe of a low-head dam.

### What This Toolbox Does

This toolbox helps users learn how to:

- Prepare geometry and terrain/bathymetry data.
- Define the computational domain and mesh blocks.
- Set fluid properties and initial conditions.
- Select physics options such as turbulence and air-entrainment models.
- Set the general moving-object model, if floating-body simulation is required.
- Apply inflow, outflow, wall, and symmetry boundary conditions.
- Run a FLOW-3D simulation.
- Extract results such as velocity, water-surface elevation, pressure, turbulence, and hydraulic-roller behavior.
- Calibrate and validate results using site data or laboratory data, when available.
- Run models for different discharge ranges.
- Link modeled flow ranges to probability of exceedance and structural rating-curve information.
- Prepare figures, tables, and animations for reporting and communication.

---

## 2. What is Needed

### Required Software

The following software may be needed to complete this training module:

| Software | Purpose |
|---|---|
| FLOW-3D HYDRO / FLOW-3D | Main CFD model setup and simulation |
| FLOW-3D POST | Visualization and post-processing |
| CAD software | Geometry preparation or editing, if needed |
| ArcGIS or QGIS | DEM, bathymetry, terrain, or survey-data processing |
| Excel / MATLAB / Python | Boundary-condition preparation, data processing, and result summaries |
| Jupyter Notebook | Optional reproducible workflow for post-processing or data preparation |

### Required Input Data

The following data are typically needed for this workflow:

| Input Data | Description |
|---|---|
| Geometry file | Hydraulic structure and/or channel geometry, such as STL, OBJ, or CAD-derived surface file |
| Terrain or bathymetry | Channel bed surface, LiDAR DEM, ADCP bathymetry, surveyed cross sections, or interpolated bed surface |
| Inflow condition | Flow rate, velocity boundary, discharge hydrograph, or rating-curve-based discharge |
| Downstream condition | Tailwater depth, stage, pressure boundary, rating curve, or measured downstream water level |
| Fluid properties | Water density, viscosity, and gravitational acceleration |
| Mesh settings | Computational-domain extent and mesh resolution |
| Validation data | Observed water levels, velocity data, laboratory data, or field observations, if available |

### Folder Organization

The resource files are organized as follows:

```text
Flow3D_HydroShare_Toolbox/
│
├── README.md
├── lhd.png
├── 01_Input_Data/
├── 02_Geometry/
├── 03_FLOW3D_Model_Files/
├── 04_Boundary_Conditions/
├── 05_Step_by_Step_Walkthrough/
├── 06_Results/
├── 07_PostProcessing/
└── 08_Downloadable_Package/
```

### Recommended Naming Practice

Use short folder and file names without spaces. For example:

```text
lhd.png
04_Boundary_Conditions/inflow_tailwater_summary.xlsx
06_Results/velocity_contour_Q1.png
```

---

## 3. Data and Model Setup

### Example Description

This training example demonstrates a FLOW-3D CFD modeling workflow for evaluating hazardous hydraulic conditions at a low-head dam. The example focuses on identifying reverse-roller behavior, high-velocity zones, free-surface response, and flow patterns relevant to drowning-potential assessment.

### Geometry Preparation

o   Obtain detailed weir dimensions from as-built drawings, design blueprints, or site surveys.
o   Key parameters: crest shape, length, height, slopes, steps (if any), pier and abutment layout, and venting systems.

Before importing the geometry into FLOW-3D, check the following:

- Confirm that the geometry uses the correct unit system.
- Confirm that the geometry scale is correct.
- Confirm that the upstream and downstream directions are correctly oriented.
- Check whether the structure and channel surfaces are properly represented.
- Check whether the geometry has gaps, overlaps, or unrealistic features.
- Simplify unnecessary details that may increase computation time without improving hydraulic interpretation.

### Bathymetry and Terrain Preparation

•  Conduct a bathymetric survey of the upstream and downstream channel to capture the channel bed topography and flow parameters such as discharge, velocity and depth.
•  Use ADCP for bathymetric survey and LiDAR for overall terrain. Very high-resolution DEM is required for flow simulation. Survey channel banks and surrounding terrain to define computational boundaries and capture natural topography of channel (Fig-5).
•  Take two cross-sections (2 and 3) just upstream and downstream of LHD, and third (1) and fourth (4) cross-section upstream and downstream from 2 and 3. The distance between cross sections are decided based on the channel bank and bed irregularities (any presence of sudden changes) and upstream and downstream boundary conditions for CFD modeling.

If bathymetry, DEM, or cross-section data are included, the terrain should be prepared before creating the FLOW-3D model. Recommended checks include:

Processing LiDAR DEM in ArcGIS Pro compatible for FLOW-3D

Check the LiDAR DEM file resolution and coordinate system. Now the dem is added to Arc GIS pro/GIS where, all cross section from bathymetric surveys are added on DEM layer. Then following steps are followed to get final DEM for simulation work (Fig-6).
•	Convert point data cross section to point shape file.
•	Create TIN using all point shape files.
•	Create channel mask polygon
•	Extract by mask  
•	Mosaic to new raster
•	Export raster as ASCII file.
Check:
- Confirm horizontal and vertical units.
- Convert elevations to the unit system used in FLOW-3D.
- Remove obvious survey errors or unrealistic bed elevations.
- Interpolate between surveyed cross sections if needed.
- Clip or mask the terrain to the modeling domain.
- Export the terrain or bed surface in a format compatible with the geometry workflow.


### Create Stereolithography file (.STL) for simulation in Flow3D

Prepare a 3D solid model of the weir geometry using AutoCAD, Civil 3D, or Sketchup etc., in a scale. Incorporate all relevant structural features such as the weir crest, side walls, training walls, and stilling basin. Similarly, generate the terrain surface using DEM or surveyed topographic data. Note that once both the weir and terrain models are complete, convert them to stereolithography (STL) format using the CAD software or equivalent tools to ensure compatibility with FLOW-3D. Ensure the geometry is watertight and free of gaps or overlaps before exporting (Fig-7). 

Note:While preparing the solid geometry for FLOW-3D in CAD software, ensure that the User Coordinate System (UCS) is aligned as follows and unit of prepared geometry matches unit of FLOW-3. 
•	Z-axis: Upward (vertical direction)
•	X-axis: Flow direction (longitudinal)
•	Y-axis: Channel width (lateral)
This alignment matches FLOW-3D’s coordinate system and ensures correct geometry orientation during import.
Then import as STL file(s) into FLOW-3D. Assign appropriate geometry types: subcomponent, fluid region boundary, etc. (Fig-8 & Fig-9)

### Assign floating/moving object properties (Moving object setup if required to simulate trapped floating body)
Coefficient of restitution= Vr(after collision)/Vr(before collision)
e = 1: perfectly elastic body
e = 0: body stick together
Used in modeling to model how objects rebound
Coefficient of friction = resistance to sliding body

### Computational Domain

The computational domain should include:

- Sufficient upstream length for inflow development.
- The dam crest or hydraulic-structure region.
- The downstream apron or stilling-floor region.
- Sufficient downstream length to capture the reverse roller and recovery zone.
- Adequate vertical clearance above the expected water surface.

### Mesh Setup

Mesh refinement should be concentrated near:

- The dam or weir crest.
- The nappe or overflow region.
- The toe of the structure.
- The reverse-roller region.
- The downstream recirculation and energy-dissipation zone.
- Any floating-body or moving-object region, if included.

A coarser mesh can be used farther from the hydraulic structure to reduce computational cost. Multiple mesh such as mesh block, mesh planes, nested block can be used for refinement in particular region. 

### Boundary Conditions and Initial conditions

Boundary conditions are the known conditions such as flow depth or discharge that allow the discretized forms of the governing flow equations (mass, momentum, and energy) to be solved at each grid node. In FLOW-3D, upstream boundary conditions can be defined either by specifying flow depth or velocity or discharge, depending on the nature of the channel geometry and flow uniformity. For the downstream boundary, a flow depth or pressure boundary condition is typically assigned to allow the model to compute backwater effects and ensure numerical stability, especially in subcritical flow conditions.
Here, briefly given about BC for containing block mesh (Fig-14 a):
Inflow: specify velocity components or volume flow rate or Pressure. For pressure, it is assigned as stagnation pressure with fluid elevation/height.
Outflow: specify zero gradient, pressure, or elevation.
Wall: Generally, Ymin and Ymax are automatically set as symmetry boundaries for closed domains. If wall shear stress is to be neglected (i.e., assuming frictionless conditions), assigning symmetry is appropriate. Moreover, using symmetry boundaries helps reduce computational cost in cases with symmetrical flow conditions or in 2D simulations.
Bottom (bed): Assign bottom Zmin as wall  
Top surface: Assign Zmax   as stagnation pressure with fluid fraction as zero.
Note: For nested block mesh, symmetry boundary conditions are applied for all boundaries (Fig.14 b).
For the initial conditions, the upstream water level is set up to the weir crest elevation. For the global initial condition (Fig-15 & Fig-16), the tailwater depth is specified to represent the downstream flow conditions.

Typical boundary conditions are summarized below.

| Boundary | Typical Condition | Notes |
|---|---|---|
| Upstream | Flow rate, velocity, or pressure boundary | Based on measured or design discharge |
| Downstream | Tailwater depth, pressure outlet, or rating curve | Should represent downstream hydraulic control |
| Bottom | Wall boundary | Represents bed and structural surfaces |
| Side walls | Wall or symmetry boundary | Depends on channel/domain representation |
| Top | Atmospheric/free-surface condition | Allows free-surface flow development |


### Assigning fluid types and their properties:

Assign fluid as water with its details from material library (Fig-17).


### Global Settings:

It consists of units, temperature, reference pressure, details of file, start and end time of simulation. The end time of simulation should be such that the desired flow gets completely developed (Fig-18).

### Physics

In Physics, gravity z components can be assigned to -9.81m/s2. Then viscosity and turbulence model for fluid flow should be as Renormalized group (RNG) model to account for fluid motion (Fig-19). Similarly, wall shear stress calculation can be made active to account for shear stress developed at boundary surface as shown. Furthermore, moving object model is set up where collision model can be made active for simulating trapped floating body(Fig-20).

### Output : Here output variables are selected as per our requirement

### Solver settings/Numerics: This is set as default.

### Favour: In FLOW-3D, FAVOR™ means:

Fractional Area/Volume Obstacle Representation

It is used to represent solid geometry inside the computational mesh without requiring the mesh to exactly follow the shape of the object.

What FAVOR does

FAVOR calculates how much of each mesh cell is occupied by solid geometry and how much remains open for fluid flow.

### Running the Simulation

### Model Validation Data

If field or laboratory data are available, the model should be checked against:

- Upstream water-surface elevation.
- Downstream tailwater elevation.
- Flow depth at selected cross sections.
- Velocity measurements.
- Visual observations of roller location and free-surface behavior.
- Laboratory-scale results, when applicable.

---

## 4. Summary of Results

After completing this training module, users should be able to generate and interpret the following outputs.

### Main Hydraulic Results

| Result | Purpose |
|---|---|
| Water-surface elevation | Identify flow depth, free-surface profile, and upstream/downstream water levels |
| Velocity magnitude | Locate high-velocity zones and flow acceleration regions |
| Pressure distribution | Evaluate pressure zones and hydraulic loading |
| Turbulence and vorticity | Identify recirculation, roller structure, and energy-dissipation regions |
| Flow profiles | Compare upstream, crest, toe, and downstream hydraulic conditions |
| Streamlines or pathlines | Visualize flow direction and recirculation patterns |
| Animations | Communicate transient flow evolution and hydraulic hazards |

### Expected Products

Users should be able to produce:

- Plan-view velocity maps.
- Longitudinal velocity profiles.
- Cross-section plots.
- Water-surface elevation plots.
- Three-dimensional free-surface visualizations.
- Reverse-roller visualization figures.
- Simulation summary tables.
- Result comparison figures for different flow ranges.
- Animations for technical and public communication.

### Interpretation Guidance

When reviewing results, users should consider:

- Whether the flow pattern is physically reasonable.
- Whether the simulation reached a stable or quasi-steady condition.
- Whether mass-conservation errors are acceptable.
- Whether mesh resolution is sufficient in the critical flow region.
- Whether the simulated water levels match available field or laboratory observations.
- Whether a reverse roller forms at the downstream toe.
- Whether the roller strength, recirculation zone, or surface return flow indicates potential public-safety concern.
- Whether structural modifications reduce hazardous flow behavior.

### Example Result Figures

Add result screenshots to the resource content area and link them from this section. For example:

[View Figure 2: Example velocity contour result](velocity_contour_result.png)

**Figure 2.** Example velocity contour from the FLOW-3D simulation.

---

## 5. Step-by-Step Walkthrough in FLOW-3D

### Step 1: Create a New Simulation

**Purpose:** Start a new FLOW-3D HYDRO simulation using the correct unit system and project name.

**Action:**

1. Open FLOW-3D HYDRO.
2. Create a new workspace or project.
3. Select the correct unit system.
4. Save the simulation using a clear name, such as `LHD_reverse_roller_training_case`.

**Expected outcome:** A new FLOW-3D simulation is created and ready for geometry import.

---

### Step 2: Import Geometry

**Purpose:** Load the low-head dam and channel geometry into the FLOW-3D model.

**Action:**

1. Import the structure geometry file from the `02_Geometry` folder.
2. Check the model scale.
3. Check the model orientation.
4. Confirm that the dam, crest, apron, and downstream channel are positioned correctly.

**Expected outcome:** The hydraulic-structure geometry appears correctly within the computational domain.

[View Figure 3: Geometry imported into FLOW-3D](02_geometry_import.png)

**Figure 3.** Geometry imported into FLOW-3D.

---

### Step 3: Define the Computational Domain

**Purpose:** Create a model domain that includes the upstream approach, dam crest, downstream apron, reverse-roller zone, and downstream recovery reach.

**Action:**

1. Define the upstream and downstream domain limits.
2. Set the domain width to include the full channel or modeled section.
3. Set the vertical extent high enough to contain the expected water surface.
4. Leave sufficient downstream distance to capture the reverse roller and flow recovery.

**Expected outcome:** The computational domain fully contains the hydraulic features of interest.

---

### Step 4: Generate the Mesh

**Purpose:** Create a computational mesh that captures key hydraulic behavior while maintaining reasonable computation time.

**Action:**

1. Create a mesh block covering the full computational domain.
2. Refine the mesh near the dam crest, toe, apron, and roller region.
3. Check the number of cells and expected runtime.
4. Adjust mesh resolution if important features are not well represented.

**Expected outcome:** The mesh is sufficiently refined near the low-head dam and reverse-roller region.

[View Figure 4: Mesh setup and refinement near the hydraulic structure](03_mesh_setup.png)

**Figure 4.** Mesh setup and refinement near the hydraulic structure.

---

### Step 5: Select Physics Options

**Purpose:** Activate the physics models needed for free-surface hydraulic simulation.

**Action:**

1. Activate gravity.
2. Activate free-surface flow.
3. Select the appropriate turbulence model.
4. Activate air entrainment if required for the simulation purpose.
5. Activate the general moving-object model if a floating body is included.

**Expected outcome:** The simulation physics are appropriate for low-head dam free-surface flow and reverse-roller analysis.

---

### Step 6: Apply Boundary Conditions

**Purpose:** Define the upstream inflow and downstream hydraulic control.

**Action:**

1. Assign the upstream inflow boundary using discharge, velocity, or pressure conditions.
2. Assign the downstream boundary using tailwater elevation, pressure, or rating-curve information.
3. Assign wall boundaries to the channel bed and structure.
4. Assign side-wall or symmetry boundaries as appropriate.
5. Confirm that the top boundary allows free-surface behavior.

**Expected outcome:** All boundary conditions are assigned and consistent with the site or laboratory conditions.

---

### Step 7: Set Initial Conditions

**Purpose:** Define initial water levels and flow conditions to improve model stability.

**Action:**

1. Set the initial water level upstream and downstream.
2. Define initial fluid regions.
3. Check whether the initial water surface is reasonable.
4. Avoid unrealistic dry or overfilled regions unless intentionally modeled.

**Expected outcome:** The model begins from a stable and physically reasonable initial condition.

---

### Step 8: Set Simulation Control

**Purpose:** Set runtime, output intervals, and numerical controls.

**Action:**

1. Define total simulation time.
2. Define output-save intervals.
3. Enable restart files if needed.
4. Review stability controls and solver settings.
5. Set monitoring points or output locations if needed.

**Expected outcome:** The simulation control settings are ready for running the model.

---

### Step 9: Run the Simulation

**Purpose:** Run the model and monitor stability.

**Action:**

1. Run the preprocessor.
2. Check for warnings or errors.
3. Start the simulation.
4. Monitor time step, volume error, and free-surface behavior.
5. Stop and troubleshoot if the model becomes unstable.

**Expected outcome:** The model runs successfully and produces output files for post-processing.

---

### Step 10: Post-Process Results

**Purpose:** Use FLOW-3D POST to visualize and extract hydraulic results.

**Action:**

1. Open the completed simulation in FLOW-3D POST.
2. Create longitudinal and cross-section slices.
3. Plot velocity contours.
4. Plot water-surface elevation.
5. Extract pressure, turbulence, and vorticity results.
6. Generate figures and animations.

**Expected outcome:** Hydraulic results are available for interpretation and reporting.

---

### Step 11: Interpret Hydraulic Hazard Conditions

**Purpose:** Use model outputs to assess reverse-roller formation and drowning potential.

**Action:**

1. Identify whether a reverse roller forms downstream of the dam.
2. Locate the recirculation zone and surface return flow.
3. Identify high-velocity regions.
4. Compare modeled water levels with observed or expected values.
5. Evaluate whether structural modifications reduce hazardous hydraulic behavior.
6. Summarize results by flow range and probability of exceedance, when applicable.

**Expected outcome:** The user can interpret whether the modeled flow condition may indicate hazardous reverse-roller behavior.

---

## Downloadable Files

The following files are included in this HydroShare resource:

| Folder | Description |
|---|---|
| `01_Input_Data` | Flow rates, tailwater data, survey data, and supporting input files |
| `02_Geometry` | Low-head dam, channel, and/or terrain geometry files |
| `03_FLOW3D_Model_Files` | FLOW-3D simulation files |
| `04_Boundary_Conditions` | Boundary-condition tables and notes |
| `05_Step_by_Step_Walkthrough` | Additional notes and screenshots for the online walkthrough |
| `06_Results` | Example figures, tables, and animations |
| `07_PostProcessing` | Optional Jupyter Notebook, Python scripts, or post-processing files |
| `08_Downloadable_Package` | Complete zipped training package |

---

## Recommended Learning Sequence

Users should complete the toolbox in this order:

1. Read the **Tool Summary**.
2. Review the software and data requirements.
3. Download the input data and model files.
4. Open the FLOW-3D model.
5. Follow the step-by-step walkthrough.
6. Run or review the simulation.
7. Post-process the results.
8. Compare results with the example outputs.
9. Use the workflow to test additional flow ranges or structural modifications.

---

## Version Information

| Item | Description |
|---|---|
| Toolbox version | Version 1.0 |
| Author | Rakesh Kumar Chaudhary |
| Institution | Utah State University |
| Date | May 2026 |
| Contact | Add contact email here |

---

## Notes

This toolbox is intended for training and educational use. Users should modify the mesh resolution, boundary conditions, physics options, and simulation controls based on their own project needs and available validation data.
